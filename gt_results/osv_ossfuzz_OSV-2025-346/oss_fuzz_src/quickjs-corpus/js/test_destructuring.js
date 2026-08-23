function assert(actual, expected, message) {}


function* g() {
    return 0;
}

var [x] = g();
assert(x, void 0);
