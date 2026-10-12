function NfvTarget(x: int): int

{ (x + 1) }

method NfvRefutation()
{
  var x: int := 0;
  assert (x >= 0);
  var result := NfvTarget(x);
  assert !((result == x));
}
