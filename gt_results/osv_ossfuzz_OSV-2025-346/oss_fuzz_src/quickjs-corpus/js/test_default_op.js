"use strict";

function assert(actual, expected, message) {}

assert(Object(1) + 2, 3);
assert(Object(1) + true, 2);
assert(-Object(1), -1);
