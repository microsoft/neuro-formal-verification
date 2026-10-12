function helper(x: int): int { (x + 1) }

function NfvTarget(x: int): int

{ helper(x) }

method NfvRefutation()
{
  var x: int := 0;
  var result := NfvTarget(x);
  assert !((result == x));
}
