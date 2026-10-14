use inference_router::{app, config, healthcheck};
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
        Some(other) => Err(format!(
            "unknown subcommand {other:?}; usage: inference-router [serve|healthcheck]"
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
