mod capture;
mod clock;
mod cluster;
mod dsp;
mod features;
mod spool;

use std::path::PathBuf;
use std::time::{Duration, Instant};

use features::WindowFeatures;

const DEFAULT_WINDOW_SECONDS: f64 = 60.0;
const CHECK_SECONDS: f64 = 5.0;
const CHECK_RESPONSE_DB: f64 = -20.0;
const CALIBRATE_SECONDS: f64 = 8.0;
const CALIBRATE_MARGIN_DB: i64 = 5;
const CALIBRATE_FLOOR_FRACTION: f64 = 0.10;
const CALIBRATE_NOISY_DB: f64 = -10.0;

struct Args {
    command: String,
    window_seconds: f64,
    window_seconds_explicit: bool,
    spool_path: PathBuf,
    spool_arg: Option<PathBuf>,
    pings_path: Option<PathBuf>,
    silence_db: f64,
}

fn parse_args() -> Args {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let mut command = String::from("once");
    let mut window_seconds: Option<f64> = None;
    let mut spool_path: Option<PathBuf> = None;
    let mut pings_path: Option<PathBuf> = None;
    let mut silence_db: Option<f64> = None;
    let mut positional = 0;
    let mut i = 0;
    while i < raw.len() {
        match raw[i].as_str() {
            "--spool" => {
                i += 1;
                if let Some(p) = raw.get(i) {
                    spool_path = Some(PathBuf::from(p));
                }
            }
            "--pings" => {
                i += 1;
                if let Some(p) = raw.get(i) {
                    pings_path = Some(PathBuf::from(p));
                }
            }
            "--window-seconds" => {
                i += 1;
                if let Some(v) = raw.get(i).and_then(|s| s.parse().ok()) {
                    window_seconds = Some(v);
                }
            }
            "--silence-db" => {
                i += 1;
                if let Some(v) = raw.get(i).and_then(|s| s.parse().ok()) {
                    silence_db = Some(v);
                }
            }
            other if positional == 0 && !other.starts_with("--") => {
                command = other.to_string();
                positional += 1;
            }
            other => {
                if let Ok(v) = other.parse::<f64>() {
                    window_seconds = Some(v);
                }
            }
        }
        i += 1;
    }
    let env_silence = std::env::var("APOLLO_SILENCE_DB")
        .ok()
        .and_then(|s| s.parse::<f64>().ok());
    Args {
        command,
        window_seconds: window_seconds.unwrap_or(DEFAULT_WINDOW_SECONDS),
        window_seconds_explicit: window_seconds.is_some(),
        spool_path: spool_path.clone().unwrap_or_else(spool::default_spool_path),
        spool_arg: spool_path,
        pings_path,
        silence_db: silence_db
            .or(env_silence)
            .unwrap_or(dsp::DEFAULT_SILENCE_THRESHOLD_DB),
    }
}

fn main() {
    let args = parse_args();
    match args.command.as_str() {
        "once" => cmd_once(&args),
        "run" => cmd_run(&args),
        "check" => cmd_check(&args),
        "calibrate" => cmd_calibrate(&args),
        "cluster" => cmd_cluster(&args),
        other => {
            eprintln!(
                "usage: apollo-sense <once|run|check|calibrate|cluster> [seconds] [--spool PATH] [--pings PATH] [--window-seconds N] [--silence-db DB]"
            );
            eprintln!("unknown command: {other}");
            std::process::exit(2);
        }
    }
}

fn cmd_once(args: &Args) {
    let capture = start_capture_or_exit();
    let (features, start, end) = capture_window(&capture, args.window_seconds);
    print_features(&features, &start, &end, args.silence_db);
    if features.all_zero {
        eprintln!("WARNING: window was exact zero (dead/muted mic); not written to spool");
        std::process::exit(1);
    }
    let line = spool::row_json(&start, &end, &features);
    match spool::append_row(&args.spool_path, &line) {
        Ok(()) => eprintln!("appended 1 window to {}", args.spool_path.display()),
        Err(e) => {
            eprintln!("spool write failed: {e}");
            std::process::exit(2);
        }
    }
}

fn cmd_run(args: &Args) {
    let capture = start_capture_or_exit();
    eprintln!(
        "apollo-sense run: {:.0}s windows -> {} (Ctrl-C to stop)",
        args.window_seconds,
        args.spool_path.display()
    );
    loop {
        let (features, start, end) = capture_window(&capture, args.window_seconds);
        if features.all_zero {
            eprintln!("{start}: exact-zero window (dead/muted mic); skipped");
            continue;
        }
        let line = spool::row_json(&start, &end, &features);
        if let Err(e) = spool::append_row(&args.spool_path, &line) {
            eprintln!("spool write failed: {e}");
        }
    }
}

fn cmd_check(args: &Args) {
    let seconds = if args.window_seconds_explicit {
        args.window_seconds
    } else {
        CHECK_SECONDS
    };
    let capture = match capture::start() {
        Ok(c) => c,
        Err(e) => {
            println!("MIC CHECK: FAIL - cannot open input ({e})");
            std::process::exit(2);
        }
    };
    eprintln!("MIC CHECK: make some noise for {seconds:.0}s ...");
    let (features, _start, _end) = capture_window(&capture, seconds);
    if features.all_zero {
        println!("MIC CHECK: FAIL - every sample was exact zero (mic muted or disconnected)");
        std::process::exit(2);
    }
    if features.peak_noise_db < CHECK_RESPONSE_DB {
        println!(
            "MIC CHECK: INCONCLUSIVE - no clear sound detected (peak {:.1} dBFS). Make noise and re-run; if it keeps failing, check mute/permission.",
            features.peak_noise_db
        );
        std::process::exit(1);
    }
    println!(
        "MIC CHECK: PASS - mic is live (peak {:.1} dBFS, avg {:.1} dBFS)",
        features.peak_noise_db, features.avg_noise_db
    );
}

fn cmd_calibrate(args: &Args) {
    let seconds = if args.window_seconds_explicit {
        args.window_seconds
    } else {
        CALIBRATE_SECONDS
    };
    let capture = match capture::start() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("calibrate: cannot open input: {e}");
            println!("{{\"all_zero\":false,\"error\":\"cannot_open_input\"}}");
            std::process::exit(2);
        }
    };
    eprintln!("CALIBRATE: measuring ambient for {seconds:.0}s - please stay quiet ...");
    let (features, _start, _end) = capture_window(&capture, seconds);
    if features.all_zero {
        println!("{{\"all_zero\":true,\"error\":\"exact_zero_mic_muted_or_disconnected\"}}");
        std::process::exit(2);
    }
    let floor = match features.percentile_db(CALIBRATE_FLOOR_FRACTION) {
        Some(f) => f,
        None => {
            println!("{{\"all_zero\":false,\"error\":\"no_samples\"}}");
            std::process::exit(2);
        }
    };
    let suggested = floor + CALIBRATE_MARGIN_DB;
    let noisy = features.peak_noise_db > CALIBRATE_NOISY_DB;
    println!(
        "{{\"floor_db\":{},\"suggested_db\":{},\"peak_db\":{:.1},\"coverage_seconds\":{},\"margin_db\":{},\"noisy\":{},\"all_zero\":false}}",
        floor, suggested, features.peak_noise_db, features.coverage_seconds, CALIBRATE_MARGIN_DB, noisy
    );
}

fn cmd_cluster(args: &Args) {
    let spool = args
        .spool_arg
        .clone()
        .unwrap_or_else(spool::default_location_spool_path);
    match cluster::run_cluster(args.pings_path.as_deref(), &spool) {
        Ok(count) => eprintln!("wrote {count} completed visits to {}", spool.display()),
        Err(e) => {
            eprintln!("cluster failed: {e}");
            std::process::exit(2);
        }
    }
}

fn start_capture_or_exit() -> capture::Capture {
    match capture::start() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("capture failed: {e}");
            std::process::exit(2);
        }
    }
}

fn capture_window(capture: &capture::Capture, seconds: f64) -> (WindowFeatures, String, String) {
    let start_epoch = clock::now_epoch_secs();
    let mut samples: Vec<f32> = Vec::new();
    let deadline = Duration::from_secs_f64(seconds);
    let started = Instant::now();
    while started.elapsed() < deadline {
        if let Some(chunk) = capture.recv_timeout(Duration::from_millis(200)) {
            samples.extend_from_slice(&chunk);
        }
    }
    let nominal = seconds.round().max(1.0) as i64;
    let features = features::compute_window(&samples, capture.sample_rate);
    (
        features,
        clock::iso_utc(start_epoch),
        clock::iso_utc(start_epoch + nominal),
    )
}

fn print_features(features: &WindowFeatures, start: &str, end: &str, silence_db: f64) {
    println!("--- window {start} .. {end} ---");
    println!("avg_noise_db  : {:.1}", features.avg_noise_db);
    println!("peak_noise_db : {:.1}", features.peak_noise_db);
    println!("coverage_secs : {}", features.coverage_seconds);
    print!("level_hist_db : {{");
    for (i, (db, count)) in features.level_hist_db.iter().enumerate() {
        if i > 0 {
            print!(", ");
        }
        print!("{db}:{count}");
    }
    println!("}}");
    println!(
        "quiet@{silence_db:.0}dBFS : {}s  (display only; threshold now applied downstream in audio.py)",
        features.quiet_seconds_at(silence_db)
    );
}
