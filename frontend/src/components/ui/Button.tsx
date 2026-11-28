import type { ButtonHTMLAttributes, ReactNode } from "react";
export function Button({children,variant="secondary",...props}:{children:ReactNode;variant?:"primary"|"secondary"|"danger"}&ButtonHTMLAttributes<HTMLButtonElement>){return <button {...props} className={`ui-button ${variant} ${props.className??""}`}>{children}</button>}
