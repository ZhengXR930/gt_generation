function assert(actual, expected, message) {}

function f2() {
    var val = 1;

    function set(a) {
        val = a;
    }

    function get(a) {
        return val;
    }

    return {"set": set, "get": get};
}

var obj = f2();
obj.set(10);
var r;
r = obj.get();
assert(r, 10, "closure2");
