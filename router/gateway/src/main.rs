use inference_router::{app, catalog, config, healthcheck};
use std::process::ExitCode;

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

const USAGE: &str = "usage: inference-router [serve | healthcheck | check-catalog <path>]\n\n  serve (default)       run the gateway\n  healthcheck           probe the running gateway; exit 0 when healthy\n  check-catalog <path>  validate a candidate catalog against GATEWAY_CONFIG_PATH; print its revision\n  -h, --help            print this usage and exit 0\n";

#[tokio::main]
async fn main() -> ExitCode {
    // Help wins over every subcommand and is answered before any config, key or socket is touched.
    if std::env::args().skip(1).any(|a| a == "--help" || a == "-h") {
        print!("{USAGE}");
        return ExitCode::SUCCESS;
    }
    // `--build`/`--version` print the build-time id (build.rs); answered before any I/O.
    if std::env::args()
        .skip(1)
        .any(|a| a == "--build" || a == "--version")
    {
        println!("{}", env!("FLEET_BUILD_ID"));
        return ExitCode::SUCCESS;
    }
    let result = match std::env::args().nth(1).as_deref() {
        Some("healthcheck") => healthcheck::run(&env).await,
        None | Some("serve") => match config::load(&env).and_then(|s| app::state(s, &env)) {
            Ok(state) => {
                let sd = fleetkit::shutdown::install(fleetkit::shutdown::DEFAULT_DRAIN);
                app::serve_until(state, sd, |addr| {
                    eprintln!("inference-router listening on {addr}")
                })
                .await
            }
            Err(e) => Err(e),
        },
        Some("check-catalog") => check_catalog(std::env::args().nth(2)),
        Some(other) => Err(format!(
            "unknown subcommand {other:?}; usage: inference-router [serve|healthcheck|check-catalog <path>]"
        )),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("inference-router: {e}");
            ExitCode::FAILURE
        }
    }
}

/// Validate a candidate catalog against the static config without binding a port; prints the
/// revision on success. Needs only `GATEWAY_CONFIG_PATH` (no gateway or provider keys).
fn check_catalog(path: Option<String>) -> Result<(), String> {
    let path = path.ok_or("usage: inference-router check-catalog <path>")?;
    let providers = config::load_static_providers(&env)?;
    let cat = catalog::load_file(&providers, &path.into())?;
    println!("{}", cat.revision);
    Ok(())
}
