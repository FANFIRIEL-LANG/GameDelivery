import type { ReactNode } from "react";
export function Card({children,className=""}:{children:ReactNode;className?:string}){return <section className={`ui-card ${className}`}>{children}</section>}
export function EmptyState({title,description}:{title:string;description:string}){return <Card className="empty-card"><div className="empty-icon">—</div><h3>{title}</h3><p>{description}</p></Card>}
export function StatusIndicator({label,state}:{label:string;state:"available"|"unavailable"|"error"|"not_implemented"}){return <span className={`status-indicator ${state}`}><i/> {label}: {state}</span>}
