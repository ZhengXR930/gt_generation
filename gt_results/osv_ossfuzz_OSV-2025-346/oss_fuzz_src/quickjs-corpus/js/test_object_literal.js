function assert(actual, expected, message) {}

var x = 0, get = 1, set = 2;
async = 3;
a = {
    get: 2, set: 3, async: 4, get a() {
        return this.get
    }
};
assert(JSON.stringify(a), '{"get":2,"set":3,"async":4,"a":2}');
assert(a.a === 2);

a = {x, get, set, async};
assert(JSON.stringify(a), '{"x":0,"get":1,"set":2,"async":3}');
