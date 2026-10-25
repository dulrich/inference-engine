// Display formatter shared by the Serving and Router views: the null-safe em-dash rather
// than each view re-deriving its own guard. Pure, so it is testable without a DOM harness.

export function formatCount(n: number | null): string {
  return n === null ? "—" : String(n);
}
