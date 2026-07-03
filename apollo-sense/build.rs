fn main() {
    cc::Build::new()
        .file("csrc/dsp.c")
        .include("csrc")
        .compile("apollo_dsp");
    println!("cargo:rerun-if-changed=csrc/dsp.c");
    println!("cargo:rerun-if-changed=csrc/dsp.h");
}
