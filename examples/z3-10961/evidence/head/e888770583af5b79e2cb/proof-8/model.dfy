// src: src/sat/sat_solver.h:385 | void set_has_new_best_phase(bool b) { m_new_best_phase = b; }
class SatState
// synth: class body
{
// src: src/sat/sat_solver.h:385 | void set_has_new_best_phase(bool b) { m_new_best_phase = b; }
  var hasNewBestPhase: bool
// synth: class body
}
// src: src/sat/sat_solver.h:385 | void set_has_new_best_phase(bool b) { m_new_best_phase = b; }
method SatSetFlag(s: SatState, b: bool)
// synth: heap update contract
  modifies s
// synth: method body
{
// src: src/sat/sat_solver.h:385 | void set_has_new_best_phase(bool b) { m_new_best_phase = b; }
  s.hasNewBestPhase := b;
// synth: method body
}
// src: src/sat/smt/sls_solver.cpp:52 | ctx.s().set_has_new_best_phase(b);
method AdapterApplyFlag(s: SatState, b: bool)
// synth: forwarded heap update contract
  modifies s
// synth: method body
{
// src: src/sat/smt/sls_solver.cpp:52 | ctx.s().set_has_new_best_phase(b);
  SatSetFlag(s, b);
// synth: method body
}
// src: src/sat/sat_solver.h:385 | void set_has_new_best_phase(bool b) { m_new_best_phase = b; }
function sat_set_has_new_best_phase(S_pre: bool, b: bool): bool
// synth: function body
{
// src: src/sat/sat_solver.h:385 | void set_has_new_best_phase(bool b) { m_new_best_phase = b; }
  b
// synth: function body
}
// src: src/sat/smt/sls_solver.cpp:52 | ctx.s().set_has_new_best_phase(b);
method Target(S_pre: bool, b: bool) returns (result: bool)
// synth: refutation goal: negation of the translated postcondition
  ensures !((result == sat_set_has_new_best_phase(S_pre, b)))
// synth: entry body
// synth: refutation witness
  requires S_pre == true
  requires b == false
{
// src: src/sat/smt/sls_solver.cpp:52 | ctx.s().set_has_new_best_phase(b);
  var sat := new SatState;
// src: src/sat/smt/sls_solver.cpp:52 | ctx.s().set_has_new_best_phase(b);
  sat.hasNewBestPhase := S_pre;
// src: src/sat/smt/sls_solver.cpp:52 | ctx.s().set_has_new_best_phase(b);
  AdapterApplyFlag(sat, b);
// src: src/sat/smt/sls_solver.cpp:52 | ctx.s().set_has_new_best_phase(b);
  result := sat.hasNewBestPhase;
// synth: entry body
}

method NfvWitnessCheck()
{
  var S_pre: bool := true;
  var b: bool := false;
  var NfvResult0 := Target(S_pre, b);
}
