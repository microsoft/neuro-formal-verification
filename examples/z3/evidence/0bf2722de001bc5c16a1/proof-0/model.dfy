// src: src/smt/theory_seq.cpp:2278 | app* theory_seq::mk_value(expr* e) {
method Target(freshLen: int, isStringValue: bool, hasLowerBound: bool, lowerBound: int) returns (modelLen: int)
// synth: inferred input domain
  requires freshLen >= 0
// synth: refutation goal: negation of the translated postcondition
  ensures !(((hasLowerBound && lowerBound > 0) ==> modelLen >= lowerBound))
// synth: method body
// synth: refutation witness
  requires freshLen == 3
  requires isStringValue == true
  requires hasLowerBound == true
  requires lowerBound == 65537
{
// src: src/smt/theory_seq.cpp:2308 | zstring s;
  var sLen := freshLen;
// src: src/smt/theory_seq.cpp:2309 | rational lo;
  var lo := lowerBound;
// src: src/smt/theory_seq.cpp:2310 | unsigned const max_pad = 1 << 16;
  var maxPad := 65536;
// src: src/smt/theory_seq.cpp:2311 | if (m_util.str.is_string(fresh, s) && lower_bound2(e, lo) && lo.is_pos() &&
  var known := isStringValue && hasLowerBound && lo > 0;
// src: src/smt/theory_seq.cpp:2312 | lo.is_unsigned() && lo.get_unsigned() <= max_pad && rational(s.length()) < lo) {
  if known && lo < 4294967296 && lo <= maxPad && sLen < lo
// synth: branch start
  {
// src: src/smt/theory_seq.cpp:2313 | std::string padded = s.encode();
    var paddedLen := sLen;
// src: src/smt/theory_seq.cpp:2314 | unsigned need = lo.get_unsigned();
    var need := lo;
// src: src/smt/theory_seq.cpp:2315 | while (padded.length() < need)
    while paddedLen < need
// synth: loop bound
      invariant sLen <= paddedLen <= need
// synth: termination
      decreases need - paddedLen
// synth: loop body
    {
// src: src/smt/theory_seq.cpp:2316 | padded += "a";
      paddedLen := paddedLen + 1;
// synth: loop body end
    }
// src: src/smt/theory_seq.cpp:2317 | fresh = m_util.str.mk_string(zstring(padded.c_str()));
    sLen := paddedLen;
// synth: branch end
  }
// src: src/smt/theory_seq.cpp:2319 | result = fresh;
  modelLen := sLen;
// synth: method end
}

method NfvWitnessCheck()
{
  var freshLen: int := 3;
  var isStringValue: bool := true;
  var hasLowerBound: bool := true;
  var lowerBound: int := 65537;
  var NfvResult0 := Target(freshLen, isStringValue, hasLowerBound, lowerBound);
}
