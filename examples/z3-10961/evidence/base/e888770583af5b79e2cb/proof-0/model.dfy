// src: src/sat/sat_solver.h:385 | void set_has_new_best_phase(bool b) { m_new_best_phase = b; }
function sat_set_has_new_best_phase(S_pre: bool, b: bool): bool
// synth: open expected-update helper
{
// src: src/sat/sat_solver.h:385 | void set_has_new_best_phase(bool b) { m_new_best_phase = b; }
b
// synth: close expected-update helper
}
// src: src/sat/smt/sls_solver.cpp:51 | void solver::set_has_new_best_phase(bool b) {
method WorkflowEntry(S_pre: bool, b: bool, singleThread: bool) returns (result: bool)
// synth: select the captured non-SINGLE_THREAD implementation
requires !singleThread
// synth: refutation goal: negation of the translated postcondition
  ensures !((result == sat_set_has_new_best_phase(S_pre, b)))
// synth: open entry body
// synth: refutation witness
  requires S_pre == false
  requires b == true
  requires singleThread == false
{
// src: src/sat/sat_solver.h:385 | void set_has_new_best_phase(bool b) { m_new_best_phase = b; }
var underlyingFlag := S_pre;
// src: src/sat/smt/sls_solver.cpp:52 | 
result := underlyingFlag;
// src: src/sat/smt/sls_solver.cpp:53 | }
// synth: close entry body
}

method NfvWitnessCheck()
{
  var S_pre: bool := false;
  var b: bool := true;
  var singleThread: bool := false;
  var NfvResult0 := WorkflowEntry(S_pre, b, singleThread);
}
