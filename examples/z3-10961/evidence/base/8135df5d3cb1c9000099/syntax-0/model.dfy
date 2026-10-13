// src: src/sat/sat_solver.h:382 | bool get_best_phase(bool_var b) { return m_best_phase.get(b, false); }
function best_phase_value(S_pre: seq<bool>, v: int): bool
// synth: valid Boolean-variable index
  requires 0 <= v < |S_pre|
// synth: function body
{
// src: src/sat/sat_solver.h:382 | bool get_best_phase(bool_var b) { return m_best_phase.get(b, false); }
  S_pre[v]
// synth: function body
}

// src: src/sat/smt/sls_solver.cpp:55 | bool solver::get_best_phase(sat::bool_var v) {
method WorkflowEntry(S_pre: seq<bool>, v: int) returns (result: bool)
// synth: intended property applies to valid Boolean variables
  requires 0 <= v < |S_pre|
// synth: original intended property
  ensures result == best_phase_value(S_pre, v)
// synth: method body
{
// src: src/sat/smt/sls_solver.cpp:56 | return false;
  result := false;
// synth: method body
}