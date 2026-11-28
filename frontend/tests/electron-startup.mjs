import { spawn } from "node:child_process";
const command = process.platform === "win32" ? `${process.cwd()}\\node_modules\\.bin\\electron.cmd` : `${process.cwd()}/node_modules/.bin/electron`;
const child = spawn(command, ["dist-electron/main/index.js"], { stdio: "ignore", windowsHide: true, shell: process.platform === "win32" });
const exited = await new Promise(resolve => { let done=false; const timer=setTimeout(()=>{done=true;resolve(false)},4000); child.once("exit",()=>{if(!done){clearTimeout(timer);done=true;resolve(true)}}); child.once("error",()=>{if(!done){clearTimeout(timer);done=true;resolve(true)}}); });
if (exited) throw new Error("electron_exited_before_startup_window");
child.kill(); console.log("Electron startup smoke: PASS");
