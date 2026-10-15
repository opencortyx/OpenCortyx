import { redirect } from "next/navigation";

// Root → the Context Layer query page.
export default function Home() {
  redirect("/dashboard/chat");
}
