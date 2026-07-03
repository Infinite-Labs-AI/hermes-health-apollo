use std::sync::mpsc::Receiver;
use std::time::Duration;

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

pub struct Capture {
    pub sample_rate: u32,
    rx: Receiver<Vec<f32>>,
    _stream: cpal::Stream,
}

impl Capture {
    pub fn recv_timeout(&self, timeout: Duration) -> Option<Vec<f32>> {
        self.rx.recv_timeout(timeout).ok()
    }
}

pub fn start() -> Result<Capture, String> {
    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .ok_or_else(|| "no default input device".to_string())?;
    let default_config = device
        .default_input_config()
        .map_err(|e| format!("no default input config: {e}"))?;

    let sample_rate = default_config.sample_rate().0;
    let channels = default_config.channels() as usize;
    let sample_format = default_config.sample_format();
    let stream_config: cpal::StreamConfig = default_config.into();

    let (tx, rx) = std::sync::mpsc::channel::<Vec<f32>>();
    let err_fn = |e| eprintln!("stream error: {e}");

    let stream = match sample_format {
        cpal::SampleFormat::F32 => device.build_input_stream(
            &stream_config,
            move |data: &[f32], _: &_| {
                let _ = tx.send(downmix(data, channels));
            },
            err_fn,
            None,
        ),
        cpal::SampleFormat::I16 => device.build_input_stream(
            &stream_config,
            move |data: &[i16], _: &_| {
                let f: Vec<f32> = data.iter().map(|s| *s as f32 / 32768.0).collect();
                let _ = tx.send(downmix(&f, channels));
            },
            err_fn,
            None,
        ),
        cpal::SampleFormat::U16 => device.build_input_stream(
            &stream_config,
            move |data: &[u16], _: &_| {
                let f: Vec<f32> = data.iter().map(|s| (*s as f32 - 32768.0) / 32768.0).collect();
                let _ = tx.send(downmix(&f, channels));
            },
            err_fn,
            None,
        ),
        other => return Err(format!("unsupported sample format: {other:?}")),
    };

    let stream = stream.map_err(|e| format!("build stream failed: {e}"))?;
    stream.play().map_err(|e| format!("stream play failed: {e}"))?;

    Ok(Capture {
        sample_rate,
        rx,
        _stream: stream,
    })
}

fn downmix(interleaved: &[f32], channels: usize) -> Vec<f32> {
    if channels <= 1 {
        return interleaved.to_vec();
    }
    let frames = interleaved.len() / channels;
    let mut mono = Vec::with_capacity(frames);
    for frame in 0..frames {
        let mut acc = 0.0f32;
        for c in 0..channels {
            acc += interleaved[frame * channels + c];
        }
        mono.push(acc / channels as f32);
    }
    mono
}
