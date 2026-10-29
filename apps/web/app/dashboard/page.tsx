import { redirect } from "next/navigation";

/** `/dashboard` had no page (a 404 from the address bar and from links): it opens the Assistant. */
export default function DashboardHome() {
  redirect("/dashboard/chat");
}
