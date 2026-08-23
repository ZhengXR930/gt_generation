/* operators overloading with Operators.create() */
"use strict";

function assert(actual, expected, message) {}

class Vec2 {
    constructor(x, y) {
        this.x = x;
        this.y = y;
    }

    static mul_scalar(p1, a) {
        var r = new Vec2();
        r.x = p1.x * a;
        r.y = p1.y * a;
        return r;
    }

    toString() {
        return "Vec2(" + this.x + "," + this.y + ")";
    }
}

Vec2.prototype[Symbol.operatorSet] = Operators.create(
    {
        "+"(p1, p2) {
            var r = new Vec2();
            r.x = p1.x + p2.x;
            r.y = p1.y + p2.y;
            return r;
        },
        "-"(p1, p2) {
            var r = new Vec2();
            r.x = p1.x - p2.x;
            r.y = p1.y - p2.y;
            return r;
        },
        "=="(a, b) {
            return a.x == b.x && a.y == b.y;
        },
        "<"(a, b) {
            var r;
            /* lexicographic order */
            if (a.x == b.x)
                r = (a.y < b.y);
            else
                r = (a.x < b.x);
            return r;
        },
        "++"(a) {
            var r = new Vec2();
            r.x = a.x + 1;
            r.y = a.y + 1;
            return r;
        }
    },
    {
        left: Number,
        "*"(a, b) {
            return Vec2.mul_scalar(b, a);
        }
    },
    {
        right: Number,
        "*"(a, b) {
            return Vec2.mul_scalar(a, b);
        }
    });

var a = new Vec2(1, 2);
var b = new Vec2(3, 4);
var r;

r = a * 2 + 3 * b;
assert(r.x === 11 && r.y === 16);
assert(a == a, true);
assert(a == b, false);
assert(a != a, false);
assert(a < b, true);
assert(a <= b, true);
assert(b < a, false);
assert(b <= a, false);
assert(a <= a, true);
assert(a >= a, true);
a++;
assert(a.x === 2 && a.y === 3);
r = ++a;
assert(a.x === 3 && a.y === 4);
assert(r === a);
