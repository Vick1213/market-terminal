#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  let builder = tauri::Builder::default();

  // The sidecar concept (bundled Python API child process) only exists on
  // desktop; guarded separately from `#[cfg(desktop)] mod sidecar` below so
  // this still compiles for a hypothetical future mobile target.
  #[cfg(desktop)]
  let builder = builder.manage(sidecar::SidecarProcess::default());

  let app = builder
    .setup(|app| {
      // Registered unconditionally (not just for debug builds) so that
      // release/bundled builds also emit the sidecar spawn/health/kill log
      // lines below. In release builds tauri-plugin-log's default targets
      // include a rotating logfile in app_log_dir(), which lands the spawn
      // diagnostics right next to the sidecar's own api.log.
      app.handle().plugin(
        tauri_plugin_log::Builder::default()
          .level(log::LevelFilter::Info)
          .build(),
      )?;

      #[cfg(desktop)]
      sidecar::spawn(app.handle());

      Ok(())
    })
    .build(tauri::generate_context!())
    .expect("error while building tauri application");

  app.run(|_app_handle, _event| {
    #[cfg(desktop)]
    if let tauri::RunEvent::Exit = _event {
      sidecar::kill(_app_handle);
    }
  });
}

/// Spawns and manages the bundled `market-api` Python sidecar (see
/// `apps/api` + `apps/desktop/scripts/build-sidecar.mjs`, which stages a
/// PyInstaller onedir build at `src-tauri/sidecar/market-api/` and, via
/// `tauri.conf.json`'s `bundle.resources`, into the packaged app's resource
/// dir under `market-api/`).
///
/// Desktop-only: there is no sidecar concept on mobile, and
/// `std::process::Command` child-process spawning isn't available there
/// either.
#[cfg(desktop)]
mod sidecar {
  use std::{
    fs,
    io,
    net::TcpStream,
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::Duration,
  };

  use tauri::Manager;

  /// Port the bundled API listens on. Passed to the child via
  /// `MARKET_SIDECAR_PORT`; must match the `NEXT_PUBLIC_API_URL`/
  /// `NEXT_PUBLIC_WS_URL` baked into the static export at build time (see
  /// `apps/web/next.config.mjs`).
  const PORT: u16 = 8765;

  fn addr() -> String {
    format!("127.0.0.1:{PORT}")
  }

  /// True if something is already accepting TCP connections on `PORT`.
  fn tcp_alive(timeout: Duration) -> bool {
    match addr().parse() {
      Ok(socket_addr) => TcpStream::connect_timeout(&socket_addr, timeout).is_ok(),
      Err(_) => false,
    }
  }

  /// Tracks the spawned sidecar child process (if any) so it can be killed
  /// on app exit. `None` covers: running under `tauri dev`, an external API
  /// already listening on `PORT`, the sidecar binary being missing, or the
  /// spawn itself failing — in every case there's simply nothing to kill
  /// later.
  #[derive(Default)]
  pub struct SidecarProcess(Mutex<Option<Child>>);

  /// Spawns the sidecar unless running under `tauri dev` or an API is
  /// already reachable on `PORT`. Never panics: every failure is logged and
  /// left for the UI to surface as fetch errors, same as any other
  /// unreachable-API state.
  pub fn spawn(app: &tauri::AppHandle) {
    // Under `tauri dev` the UI talks to a manually-run dev API on port 8000
    // (see apps/desktop/README.md) and resource_dir() would resolve next to
    // the dev cargo build, not the staged sidecar dir, so there's nothing to
    // spawn here.
    if tauri::is_dev() {
      log::info!(
        "tauri dev detected: not spawning the market-api sidecar (expects the manually-run dev API on 127.0.0.1:8000)"
      );
      return;
    }

    let resource_dir = match app.path().resource_dir() {
      Ok(dir) => dir,
      Err(err) => {
        log::error!("could not resolve the app resource dir, cannot locate the market-api sidecar: {err}");
        return;
      }
    };

    let sidecar_dir = resource_dir.join("market-api");
    let exe_name = if cfg!(windows) { "market-api.exe" } else { "market-api" };
    let sidecar_path = sidecar_dir.join(exe_name);

    if !sidecar_path.is_file() {
      log::error!(
        "market-api sidecar not found at {} — the UI will show fetch errors until an API is reachable on {}",
        sidecar_path.display(),
        addr()
      );
      return;
    }

    if tcp_alive(Duration::from_millis(300)) {
      log::info!("external API already listening on {}, not spawning the sidecar", addr());
      return;
    }

    let mut command = Command::new(&sidecar_path);
    command
      .current_dir(&sidecar_dir)
      .env("MARKET_SIDECAR_PORT", PORT.to_string());

    match open_log_stdio(app) {
      Ok((stdout, stderr)) => {
        command.stdout(stdout).stderr(stderr);
      }
      Err(err) => {
        log::warn!("could not open the sidecar log file, its stdout/stderr will be discarded: {err}");
        command.stdout(Stdio::null()).stderr(Stdio::null());
      }
    }

    match command.spawn() {
      Ok(child) => {
        log::info!("spawned market-api sidecar (pid {})", child.id());
        *app.state::<SidecarProcess>().0.lock().unwrap() = Some(child);
        spawn_readiness_probe();
      }
      Err(err) => {
        log::error!("failed to spawn market-api sidecar at {}: {err}", sidecar_path.display());
      }
    }
  }

  /// Opens (creating the dir/file as needed) `<app log dir>/api.log` in
  /// append mode and returns two independent `Stdio` handles — one for the
  /// child's stdout, one for its stderr — both writing to that same file.
  fn open_log_stdio(app: &tauri::AppHandle) -> io::Result<(Stdio, Stdio)> {
    let log_dir = app
      .path()
      .app_log_dir()
      .map_err(|err| io::Error::other(err.to_string()))?;
    fs::create_dir_all(&log_dir)?;

    let log_path = log_dir.join("api.log");
    let stdout_file = fs::OpenOptions::new()
      .create(true)
      .append(true)
      .open(&log_path)?;
    let stderr_file = stdout_file.try_clone()?;
    Ok((Stdio::from(stdout_file), Stdio::from(stderr_file)))
  }

  /// Background readiness poll: logs once the sidecar starts accepting
  /// connections, or logs a timeout after ~60s. Runs on its own thread so it
  /// never blocks `setup`/window creation.
  fn spawn_readiness_probe() {
    thread::spawn(|| {
      let deadline = Duration::from_secs(60);
      let interval = Duration::from_millis(500);
      let mut waited = Duration::ZERO;

      while waited < deadline {
        if tcp_alive(Duration::from_millis(300)) {
          log::info!("market-api sidecar is ready on {}", addr());
          return;
        }
        thread::sleep(interval);
        waited += interval;
      }

      log::warn!(
        "market-api sidecar did not start accepting connections on {} within {}s",
        addr(),
        deadline.as_secs()
      );
    });
  }

  /// Kills the sidecar child process (if we spawned one) on app exit.
  ///
  /// Called from the `RunEvent::Exit` arm in `run()`, which fires once on
  /// the way out — after it returns, Tauri runs `AppHandle::cleanup_before_exit`
  /// and the underlying event loop terminates the process — so this runs
  /// synchronously before the process actually exits (see
  /// `App::make_run_event_loop_callback` in the `tauri` crate).
  pub fn kill(app: &tauri::AppHandle) {
    let Some(mut child) = app.state::<SidecarProcess>().0.lock().unwrap().take() else {
      return;
    };

    if let Err(err) = child.kill() {
      log::warn!("failed to kill the market-api sidecar: {err}");
    }
    if let Err(err) = child.wait() {
      log::warn!("failed to wait on the market-api sidecar after kill: {err}");
    }
  }
}
