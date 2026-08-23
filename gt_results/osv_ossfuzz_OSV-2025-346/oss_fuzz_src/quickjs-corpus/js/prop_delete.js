var ref, obj, j, k;
ref = {a: 1, b: 2, c: 3, d: 4, e: 5, f: 6, g: 7, h: 8, i: 9, j: 10};
for (k = 0; k < 10; k++) {
    ref[k] = k;
}
obj = {...ref};
delete obj.a;
delete obj.b;
delete obj.c;
delete obj.d;
delete obj.e;
delete obj.f;
delete obj.g;
delete obj.h;
delete obj.i;
delete obj.j;
for (k = 0; k < 10; k++) {
    delete obj[k];
}
