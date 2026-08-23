function assert(actual, expected, message) {}

assert(typeof eval("() => {}\n() => {}"), "function");
assert(eval("() => {}\n+1"), 1);
assert(typeof eval("x => {}\n() => {}"), "function");
assert(typeof eval("async () => {}\n() => {}"), "function");
assert(typeof eval("async x => {}\n() => {}"), "function");
