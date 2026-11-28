import { create } from "zustand";
export type Section = "dashboard" | "orders" | "playerok" | "operator" | "inventory" | "asf" | "network" | "telegram";
export const sections: {id: Section; label: string}[] = [
 {id:"dashboard",label:"Dashboard"},{id:"orders",label:"Orders"},{id:"playerok",label:"Playerok"},{id:"operator",label:"Operator"},{id:"inventory",label:"Inventory / Looter"},{id:"asf",label:"ASF Bots"},{id:"network",label:"Proxies & OpenSkin"},{id:"telegram",label:"Telegram Bot"}
];
type AppState = { active: Section; consoleOpen: boolean; setActive: (active: Section)=>void; toggleConsole: ()=>void };
export const useAppStore = create<AppState>((set)=>({active:"dashboard",consoleOpen:false,setActive:(active)=>set({active}),toggleConsole:()=>set((s)=>({consoleOpen:!s.consoleOpen}))}));
