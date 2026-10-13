// src: src/sat/smt/sls_solver.cpp:55 | bool solver::get_best_phase(sat::bool_var v) {
method WorkflowEntry(bestPhases: seq<bool>, v: int) returns (result: bool)
// synth: model a valid 32-bit unsigned SAT variable index
  requires 0 <= v <= 4294967295 && v < |bestPhases|
// synth: refutation goal: negation of the translated postcondition
  ensures !((bestPhases[v] == false ==> result == false))
// synth: method body
// synth: refutation witness
  requires bestPhases == [false]
  requires v == 0
{
// src: src/sat/smt/sls_solver.cpp:56 | return false;
  result := false;
// synth: method body
}

method NfvWitnessCheck()
{
  var bestPhases: seq<bool> := [false];
  var v: int := 0;
  var NfvResult0 := WorkflowEntry(bestPhases, v);
}
