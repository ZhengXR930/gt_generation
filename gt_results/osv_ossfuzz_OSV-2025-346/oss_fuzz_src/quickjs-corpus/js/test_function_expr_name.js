function assert(actual, expected, message) {}
function assert_throws(expected_error, func) {}

var f;

/* non strict mode test : assignment to the function name silently
   fails */

f = function myfunc() {
    myfunc = 1;
    return myfunc;
};
assert(f(), f);

f = function myfunc() {
    myfunc = 1;
    (() => {
        myfunc = 1;
    })();
    return myfunc;
};
assert(f(), f);

f = function myfunc() {
    eval("myfunc = 1");
    return myfunc;
};
assert(f(), f);

/* strict mode test : assignment to the function name raises a
   TypeError exception */

f = function myfunc() {
    "use strict";
    myfunc = 1;
};
assert_throws(TypeError, f);

f = function myfunc() {
    "use strict";
    (() => {
        myfunc = 1;
    })();
};
assert_throws(TypeError, f);

f = function myfunc() {
    "use strict";
    eval("myfunc = 1");
};
assert_throws(TypeError, f);
