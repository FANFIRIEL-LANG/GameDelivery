import { readFileSync } from "node:fs";
const plan=readFileSync(new URL("../../docs/frontend/LIVE_ACCEPTANCE_PLAN.md", import.meta.url),"utf8");
const record=readFileSync(new URL("../../docs/frontend/LIVE_ACCEPTANCE_RECORD_TEMPLATE.md", import.meta.url),"utf8");
for(const section of ["Steam / ASF","Playerok","Telegram","File delivery","General stop gate"]) if(!plan.includes(`## ${section}`)) throw new Error(`missing plan section: ${section}`);
for(const marker of ["Preconditions:","Scenario:","Expected result:","Verification:","Risks:","Recovery:"]) if(!plan.includes(marker)) throw new Error(`missing plan marker: ${marker}`);
for(const result of ["NOT RUN","PASS","FAIL","UNKNOWN"]) if(!record.includes(result)) throw new Error(`missing record result: ${result}`);
if(!/Stop and ask for explicit approval/i.test(plan)) throw new Error("missing live stop gate");
console.log("Live acceptance documentation smoke: PASS");
