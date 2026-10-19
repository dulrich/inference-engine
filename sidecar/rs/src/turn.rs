//! `turn.mjs` — phase 3 of the request path: `start_thread` (open a thread and VERIFY it,
//! fail closed), `deliver_tool_results` (answer parked calls), `start_turn` (run a turn).
//! `still_mine` is the supersession check after every await.

use crate::codex::CallId;
use crate::config::resolve_cwd;
use crate::errors::{as_sidecar_error, SidecarError};
use crate::messages::{superseded, Rc};
use crate::persona;
use crate::protocol::{base_instructions_of, text_of, to_dynamic_tools};
use crate::request::{renders_transcript, turn_input_text, Content};
use crate::session;
use crate::sidecar::{err, lock, Shared, World};
use crate::thread_config::{feature_readings, verify_features, verify_instruction_sources};
use serde_json::{json, Map, Value};

pub fn still_mine(w: &World, rc: &Rc) -> Result<(), SidecarError> {
    if w.sessions.get(&rc.sid).and_then(|s| s.owner_id()) == Some(rc.token_id) {
        Ok(())
    } else {
        Err(superseded())
    }
}

fn opt_str(v: &Option<String>) -> Value {
    v.as_deref().map_or(Value::Null, Value::from)
}

pub async fn start_thread(me: &Shared, rc: &Rc) -> Result<(), SidecarError> {
    let (rx, hook_before, mcp_before, cwd, n_tools) = {
        let mut w = lock(me);
        let w = &mut *w;
        let cwd = resolve_cwd(&w.cfg, &mut w.logger, rc.norm.raw_cwd.as_deref())
            .map_err(|e| as_sidecar_error(&e.to_string()))?;
        let p = &w.personas[rc.pi];
        let (hb, mb) = (p.hook_events, p.mcp_events);
        let tools = to_dynamic_tools(rc.norm.req.tool_list());
        let n_tools = tools.len();
        let params = json!({
            "cwd": cwd,
            "model": rc.ident.parsed.model,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": true,
            "baseInstructions": base_instructions_of(&w.pinned.preamble, &rc.norm.system_text, &rc.norm.interleaved_text),
            "dynamicTools": tools,
            "config": w.pinned.thread_config,
        });
        let t = w.cfg.turn_timeout_ms;
        (
            persona::rpc(w, rc.pi, "thread/start", params, t),
            hb,
            mb,
            cwd,
            n_tools,
        )
    };
    let started = rx.wait().await?;
    let rx = {
        let mut w = lock(me);
        let w = &mut *w;
        still_mine(w, rc)?;
        let tid = crate::codex::started_thread_id(&started);
        if let Some(s) = w.sessions.get_mut(&rc.sid) {
            s.thread_id = tid.clone();
            s.model = Some(rc.ident.parsed.model.clone());
        }
        let Some(tid) = tid else {
            return Err(err(502, "api_error", "thread/start returned no thread id"));
        };
        w.personas[rc.pi].bus_set(tid.clone(), rc.sid);
        let sources = started.get("instructionSources");
        let p = &w.personas[rc.pi];
        let problem = verify_instruction_sources(sources)
            .or_else(|| {
                (p.hook_events != hook_before).then(|| "a hook ran during thread/start".into())
            })
            .or_else(|| {
                (p.mcp_events != mcp_before)
                    .then(|| "an MCP server started during thread/start".into())
            });
        if let Some(problem) = problem {
            let src = sources.cloned().unwrap_or(Value::Null);
            w.log(
                "config-unverified",
                json!({ "persona": rc.persona, "threadId": tid, "stage": "thread/start", "problem": problem, "instructionSources": src }),
            );
            persona::drop_session(w, rc.pi, rc.sid);
            return Err(err(
                502,
                "api_error",
                format!("thread config not honoured: {problem}"),
            ));
        }
        let t = w.cfg.turn_timeout_ms;
        persona::rpc(
            w,
            rc.pi,
            "experimentalFeature/list",
            json!({ "threadId": tid, "limit": 500 }),
            t,
        )
    };
    let listed = rx.wait().await;
    let mut w = lock(me);
    let w = &mut *w;
    let (reported, oracle_error) = match listed {
        Ok(l) => (
            match l.get("data") {
                Some(Value::Array(a)) => Value::Array(a.clone()),
                _ => json!([]),
            },
            None,
        ),
        Err(e) => (json!([]), Some(e.message)),
    };
    let declared = w.pinned.thread_config.get("features").cloned();
    let readings = feature_readings(declared.as_ref(), Some(&reported));
    let feature_problem = match &oracle_error {
        Some(e) => Some(format!("feature oracle unavailable: {e}")),
        None => verify_features(declared.as_ref(), Some(&reported)),
    };
    let tid = w
        .sessions
        .get(&rc.sid)
        .and_then(|s| s.thread_id.as_ref())
        .map_or(Value::Null, Value::from);
    let sources = started
        .get("instructionSources")
        .filter(|v| !v.is_null())
        .cloned();
    if let Some(fp) = feature_problem {
        let mism: Vec<Value> = readings
            .mismatches
            .iter()
            .map(|m| m.entry.clone())
            .collect();
        w.log(
            "config-unverified",
            json!({ "persona": rc.persona, "threadId": tid, "stage": "thread/start", "problem": fp,
                    "instructionSources": sources.unwrap_or(Value::Null), "features": mism }),
        );
        persona::drop_session(w, rc.pi, rc.sid);
        return Err(err(
            502,
            "api_error",
            format!("thread config not honoured: {fp}"),
        ));
    }
    still_mine(w, rc)?;
    let mut row = Map::new();
    row.insert("persona".into(), Value::from(rc.persona.as_str()));
    row.insert("threadId".into(), tid);
    row.insert("key".into(), Value::from(rc.ident.key.as_str()));
    row.insert("scope".into(), Value::from(rc.ident.scope));
    row.insert("cwd".into(), Value::from(cwd));
    row.insert("model".into(), Value::from(rc.ident.parsed.model.as_str()));
    if rc.ident.eff.effort.is_some() {
        row.insert("effort".into(), opt_str(&rc.ident.eff.effort));
    }
    row.insert(
        "effortSource".into(),
        Value::from(rc.ident.eff.effort_source),
    );
    row.insert("instructionSources".into(), sources.unwrap_or(json!([])));
    row.insert("nTools".into(), Value::from(n_tools));
    row.insert(
        "preambleChars".into(),
        Value::from(w.pinned.preamble.chars().count()),
    );
    row.insert(
        "systemChars".into(),
        Value::from(rc.norm.system_text.chars().count()),
    );
    row.insert(
        "interleavedSystemChars".into(),
        Value::from(rc.norm.interleaved_text.chars().count()),
    );
    row.insert("features".into(), Value::Object(readings.features));
    row.insert(
        "unknownFeatures".into(),
        Value::from(readings.unknown_features),
    );
    row.insert(
        "multiAgentMode".into(),
        started
            .get("multiAgentMode")
            .cloned()
            .unwrap_or(Value::Null),
    );
    w.log("config-verified", Value::Object(row));
    Ok(())
}

/// Returns the number of re-sent results silently absorbed.
pub fn deliver_tool_results(w: &mut World, rc: &Rc) -> usize {
    let mut deduped = 0;
    for tr in &rc.norm.tool_results() {
        let text = match &tr.content {
            Some(Content::Text(s)) => s.clone(),
            c => {
                let t = text_of(c.as_ref());
                match c {
                    _ if !t.is_empty() => t,
                    None | Some(Content::Other(Value::Null)) => "\"\"".into(),
                    Some(c) => c.to_value().to_string(),
                }
            }
        };
        let id = tr.tool_use_id.clone().unwrap_or(Value::Null);
        let ok = !tr.is_error();
        let call = CallId(session::id_key(&id));
        if session::answer_parked(w, rc.sid, &call, &text, ok) {
            continue;
        }
        let delivered = w
            .sessions
            .get(&rc.sid)
            .is_some_and(|s| !id.is_null() && s.delivered.contains(&session::id_key(&id)));
        if delivered {
            deduped += 1;
            continue;
        }
        w.log(
            "orphan-tool-result",
            json!({ "persona": rc.persona, "key": rc.ident.key, "tool_use_id": id }),
        );
    }
    session::abandon_delivered(w, rc.sid, "no result returned by the client");
    deduped
}

pub async fn start_turn(me: &Shared, rc: &Rc) -> Result<(), SidecarError> {
    let rx = {
        let mut w = lock(me);
        let w = &mut *w;
        let n = &rc.norm;
        let text = turn_input_text(rc.mode, &n.msgs);
        if renders_transcript(rc.mode, &n.msgs) {
            w.log(
                "rehydrated",
                json!({ "persona": rc.persona, "key": rc.ident.key, "reqId": rc.req_id, "nMessages": n.msgs.len(), "chars": text.chars().count() }),
            );
        }
        let Some(s) = w.sessions.get_mut(&rc.sid) else {
            return Err(superseded());
        };
        let mut tp = Map::new();
        tp.insert(
            "threadId".into(),
            s.thread_id.as_ref().map_or(Value::Null, Value::from),
        );
        tp.insert("input".into(), json!([{ "type": "text", "text": text }]));
        if let Some(e) = &rc.ident.eff.effort {
            tp.insert("effort".into(), Value::from(e.as_str()));
        }
        let model = &rc.ident.parsed.model;
        if s.model
            .as_ref()
            .is_some_and(|m| !m.is_empty() && m != model)
        {
            tp.insert("model".into(), Value::from(model.as_str()));
            s.model = Some(model.clone());
        }
        let changed = match &s.last_effort {
            Some(prev) if *prev != rc.ident.eff.effort => Some(prev.clone()),
            _ => None,
        };
        let tid = s.thread_id.as_ref().map_or(Value::Null, Value::from);
        s.delta_items.clear();
        s.pending_outcomes.clear();
        if let Some(from) = changed {
            let mut row = Map::new();
            row.insert("persona".into(), Value::from(rc.persona.as_str()));
            row.insert("threadId".into(), tid);
            row.insert("key".into(), Value::from(rc.ident.key.as_str()));
            if from.is_some() {
                row.insert("from".into(), opt_str(&from));
            }
            if rc.ident.eff.effort.is_some() {
                row.insert("to".into(), opt_str(&rc.ident.eff.effort));
            }
            row.insert("source".into(), Value::from(rc.ident.eff.effort_source));
            w.log("effort-changed", Value::Object(row));
        }
        let t = w.cfg.turn_timeout_ms;
        persona::rpc(w, rc.pi, "turn/start", Value::Object(tp), t)
    };
    let turn = rx.wait().await?;
    let mut w = lock(me);
    if let Some(s) = w.sessions.get_mut(&rc.sid) {
        s.last_effort = Some(rc.ident.eff.effort.clone());
    }
    still_mine(&w, rc)?;
    if let Some(s) = w.sessions.get_mut(&rc.sid) {
        let t = crate::codex::started_turn_id(&turn);
        if t.is_some() {
            s.turn_id = t;
        }
    }
    Ok(())
}
