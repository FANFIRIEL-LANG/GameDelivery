import { app, BrowserWindow, ipcMain } from "electron";
import path from "node:path";
import { spawn } from "node:child_process";
let bridgeProcess: ReturnType<typeof spawn> | undefined;
let pending = "";
let bridgeBusy = false;
function startBridge() { const bridgePath = app.isPackaged ? path.join(process.resourcesPath, "backend_bridge.py") : path.join(__dirname, "../../backend_bridge.py"); bridgeProcess = spawn(process.env.PYTHON ?? "python", [bridgePath], { stdio: ["pipe", "pipe", "ignore"] }); bridgeProcess.once("error", () => { bridgeProcess = undefined; }); bridgeProcess.once("exit", () => { bridgeProcess = undefined; pending = ""; bridgeBusy = false; }); }
function stopBridge() { bridgeProcess?.kill(); bridgeProcess = undefined; pending = ""; bridgeBusy = false; }
function requestBridge(payload: object): Promise<unknown> { return new Promise((resolve, reject) => { const process = bridgeProcess; if (!process?.stdin || !process.stdout) return reject(new Error("bridge_unavailable")); if (bridgeBusy) return reject(new Error("bridge_busy")); bridgeBusy = true; let settled = false; const finish = (callback: () => void) => { if (settled) return; settled = true; bridgeBusy = false; process.stdout?.off("data", onData); clearTimeout(timer); callback(); }; const onData = (chunk: Buffer) => { pending += chunk.toString(); const index = pending.indexOf("\n"); if (index < 0) return; const line = pending.slice(0, index); pending = pending.slice(index + 1); try { const response = JSON.parse(line); finish(() => resolve(response)); } catch { finish(() => reject(new Error("bridge_invalid_response"))); } }; const timer = setTimeout(() => finish(() => reject(new Error("bridge_timeout"))), 3000); process.stdout.on("data", onData); try { process.stdin.write(JSON.stringify(payload) + "\n"); } catch { finish(() => reject(new Error("bridge_write_failed"))); } }); }
async function handleBridge(payload: object) { try { return await requestBridge(payload); } catch (error) { return { ok: false, error: error instanceof Error ? error.message : "bridge_error" }; } }
ipcMain.handle("gamedelivery:get-health", () => handleBridge({ method: "health" }));
ipcMain.handle("gamedelivery:get-capabilities", () => handleBridge({ method: "capabilities" }));
ipcMain.handle("gamedelivery:list-orders", () => handleBridge({ method: "orders.list" }));
ipcMain.handle("gamedelivery:get-operator-summary", () => handleBridge({ method: "operator.summary" }));
ipcMain.handle("gamedelivery:get-asf-status", () => handleBridge({ method: "asf.status" }));
ipcMain.handle("gamedelivery:get-asf-inventory", (_event, request: { bot: string; app_id: 753 | 730; context_id: 6 | 2 }) => handleBridge({ method: "asf.inventory", ...request }));
app.whenReady().then(() => { startBridge(); const win = new BrowserWindow({ webPreferences: { preload: path.join(__dirname, "../preload/index.js"), contextIsolation: true, nodeIntegration: false, sandbox: true } }); win.loadFile(path.join(__dirname, "../../dist/index.html")); });
app.on("before-quit", stopBridge); app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
