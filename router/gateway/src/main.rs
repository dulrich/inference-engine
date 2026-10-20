use inference_router::{app, catalog, config, healthcheck};
use std::process::ExitCode;

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

#[tokio::main]
async fn main() -> ExitCode {
    let result = match std::env::args().nth(1).as_deref() {
        Some("healthcheck") => healthcheck::run(&env).await,
        None | Some("serve") => match config::load(&env).and_then(|s| app::state(s, &env)) {
            Ok(state) => {
                app::serve(state, |addr| {
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
