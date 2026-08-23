function assert(actual, expected, message) {}

var a, err;

a = {x: 1, y: 1};
assert((delete a.x), true, "delete");
assert(("x" in a), false, "delete");

/* the following are not tested by test262 */
assert(delete "abc"[100], true);

err = false;
try {
    delete null.a;
} catch (e) {
    err = (e instanceof TypeError);
}
assert(err, true, "delete");

err = false;
try {
    a = {
        f() {
            delete super.a;
        }
    };
    a.f();
} catch (e) {
    err = (e instanceof ReferenceError);
}
assert(err, true, "delete");
