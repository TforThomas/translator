import { Link, useLocation, useNavigate } from "react-router-dom";
import { BookOpen, Home as HomeIcon, PenTool, Settings as SettingsIcon } from "lucide-react";
import { useEffect, type ReactNode } from "react";

interface LingoShellProps {
	children: ReactNode;
	greeting?: string;
}

const NAV = [
	{ to: "/",         label: "首页",   icon: HomeIcon },
	{ to: "/terms",    label: "术语集", icon: PenTool },
	{ to: "/settings", label: "设置",   icon: SettingsIcon },
];

export default function LingoShell({ children, greeting }: LingoShellProps) {
	const location = useLocation();
	const navigate = useNavigate();

	useEffect(() => {
		document.body.classList.add("lp-body");
		return () => document.body.classList.remove("lp-body");
	}, []);

	return (
		<div className="lp-shell">
			<aside className="lp-sidebar">
				<div className="lp-brand" onClick={() => navigate("/")}>
					<span className="lp-brand-mark">LingoPoet</span>
					<span className="lp-brand-sub">Omni · Translate</span>
				</div>

				<nav className="lp-nav">
					{NAV.map(({ to, label, icon: Icon }) => {
						const active = to === "/" ? location.pathname === "/" : location.pathname.startsWith(to);
						return (
							<Link key={to} to={to} className={"lp-nav-item " + (active ? "is-active" : "")}>
								<Icon /> {label}
							</Link>
						);
					})}
				</nav>

				<div className="lp-nav-bottom">
					<div className="lp-divider">Reader</div>
					<div className="lp-row lp-gap-10">
						<div className="lp-avatar-sm">兰</div>
						<div className="lp-stack">
							<span className="lp-fs-13">{greeting || "知晓"}</span>
							<span className="lp-handwrite lp-fs-13">今日安好 ✦</span>
						</div>
					</div>
				</div>
			</aside>

			<main className="lp-main">
				<BookOpen className="lp-main-icon" />
				{children}
			</main>
		</div>
	);
}