function assert(actual, expected, message) {}

let removed_key = "";
let target = {}
let proxy = new Proxy(target, {
    ownKeys: function () {
        return ["a", "b", "c"];
    },
    getOwnPropertyDescriptor: function (target, key) {
        if (removed_key != "" && key == removed_key)
            return undefined;
        else
            return {enumerable: true, configurable: true, value: this[key]};
    }
});
let str = "";
for (let o in proxy) {
    str += " " + o;
    if (o == "a")
        removed_key = "b";
}
assert(str == " a c");
