import { existsSync } from "node:fs";
for (const file of ["dist/index.html","dist-electron/main/index.js","dist-electron/preload/index.js","backend_bridge.py"]) if (!existsSync(file)) throw new Error(`missing ${file}`);
console.log("Production layout smoke: PASS");
