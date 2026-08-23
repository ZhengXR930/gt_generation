function assert(actual, expected, message) {}

var buffer, a, i, str;

a = new Uint8Array(4);
assert(a.length, 4);
for (i = 0; i < a.length; i++)
    a[i] = i;
assert(a.join(","), "0,1,2,3");
a[0] = -1;
assert(a[0], 255);

a = new Int8Array(3);
a[0] = 255;
assert(a[0], -1);

a = new Int32Array(3);
a[0] = Math.pow(2, 32) - 1;
assert(a[0], -1);
assert(a.BYTES_PER_ELEMENT, 4);

a = new Uint8ClampedArray(4);
a[0] = -100;
a[1] = 1.5;
a[2] = 0.5;
a[3] = 1233.5;
assert(a.toString(), "0,2,0,255");

buffer = new ArrayBuffer(16);
assert(buffer.byteLength, 16);
a = new Uint32Array(buffer, 12, 1);
assert(a.length, 1);
a[0] = -1;

a = new Uint16Array(buffer, 2);
a[0] = -1;

a = new Float32Array(buffer, 8, 1);
a[0] = 1;

a = new Uint8Array(buffer);

str = a.toString();
/* test little and big endian cases */
if (str !== "0,0,255,255,0,0,0,0,0,0,128,63,255,255,255,255" &&
    str !== "0,0,255,255,0,0,0,0,63,128,0,0,255,255,255,255") {
    assert(false);
}

assert(a.buffer, buffer);

a = new Uint8Array([1, 2, 3, 4]);
assert(a.toString(), "1,2,3,4");
a.set([10, 11], 2);
assert(a.toString(), "1,2,10,11");
