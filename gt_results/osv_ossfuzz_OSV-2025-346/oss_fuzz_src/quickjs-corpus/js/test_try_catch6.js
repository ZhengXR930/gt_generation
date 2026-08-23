function assert(actual, expected, message) {}

function f() {
    try {
        s += 't';
        return 1;
    } finally {
        s += "f";
    }
}

var s = "";
assert(f() === 1);
assert(s, "tf", "catch6");
