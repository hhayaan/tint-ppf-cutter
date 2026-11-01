// File-path observation only. No file contents, memory writes, or API replacement.
// Frida executes this in the one explicitly selected editor process.
const observed = new Set();
for (const [name, wide] of [['CreateFileW', true], ['CreateFileA', false]]) {
  const address = Process.getModuleByName('kernel32.dll').getExportByName(name);
  Interceptor.attach(address, {
    onEnter(args) {
      this.path = null;
      try {
        const raw = wide ? args[0].readUtf16String(2048) : args[0].readCString(2048);
        const path = raw?.split('\u0000', 1)[0];
        if (path && /\.(mdb|fdu|fda|fdc)(?:$|\s)/i.test(path)) this.path = path;
      } catch (_) { /* Unreadable arguments are not evidence of an opened file. */ }
    },
    onLeave(result) {
      if (!this.path || result.equals(ptr(-1))) return;
      const key = name + ':' + this.path;
      if (observed.has(key)) return;
      observed.add(key);
      send({event:'file-open', api:name, path:this.path, success:true});
    }
  });
}
send({event:'observer-ready', pid:Process.id, architecture:Process.arch});
setTimeout(() => { Interceptor.detachAll(); send({event:'observer-stopped', reason:'120-second limit'}); }, 120000);
