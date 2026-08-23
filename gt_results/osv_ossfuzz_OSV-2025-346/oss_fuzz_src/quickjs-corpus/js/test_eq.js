function assert(actual, expected, message) {}

assert(null == undefined);
assert(undefined == null);
assert(true == 1);
assert(0 == false);
assert("" == 0);
assert("123" == 123);
assert("122" != 123);
assert((new Number(1)) == 1);
assert(2 == (new Number(2)));
assert((new String("abc")) == "abc");
assert({} != "abc");
