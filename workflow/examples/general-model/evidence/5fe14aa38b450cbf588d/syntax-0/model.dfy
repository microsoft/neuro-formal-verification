// src: src/counter.cpp:1 | int count(int n) {
method Target(n: int) returns (r: int)
// synth: inferred input domain
  requires n >= 0
// synth: original intended property
  ensures r == n
// synth: method body
{
// src: src/counter.cpp:2 | int total = 0;
  r := 0;
// src: src/counter.cpp:3 | for (int i = 0; i < n; ++i) { ++total; }
  var i := 0;
// src: src/counter.cpp:3 | for (int i = 0; i < n; ++i) { ++total; }
  while i < n
// synth: initial loop bound
    invariant 0 <= i <= n
// synth: loop body
  {
// src: src/counter.cpp:3 | for (int i = 0; i < n; ++i) { ++total; }
    r := r + 1;
// src: src/counter.cpp:3 | for (int i = 0; i < n; ++i) { ++total; }
    i := i + 1;
// synth: loop body end
  }
// src: src/counter.cpp:4 | return total + 1;
  r := r + 1;
// synth: method end
}
