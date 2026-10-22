fn main() {
    if std::env::args().skip(1).any(|a| a == "--help" || a == "-h") {
        print!("usage: gen-ts\n\nPrint the generated TypeScript contract module to stdout (redirect into packages/shared).\n");
        return;
    }
    print!("{}", bd_domain::tsgen::module());
}
