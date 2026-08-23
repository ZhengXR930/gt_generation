/* test closure variable handling when freeing asynchronous
   function */
(async function run() {
    let obj = {}

    let done = () => {
        obj
        std.gc();
    }

    Promise.resolve().then(done)

    const p = new Promise(() => {
    })

    await p
})();
