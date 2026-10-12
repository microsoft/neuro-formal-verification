function NfvTarget(proofsEnabled: bool, preprocessEnabled: bool, skipBooleanGoal: bool): bool

{ (if proofsEnabled then false else (if !(preprocessEnabled) then false else !(skipBooleanGoal))) }

method NfvRefutation()
{
  var proofsEnabled: bool := true;
  var preprocessEnabled: bool := true;
  var skipBooleanGoal: bool := false;
  var result := NfvTarget(proofsEnabled, preprocessEnabled, skipBooleanGoal);
  assert !((!(proofsEnabled) || !(result)));
}
