import { redirect } from "next/navigation";

/** Retired route: redirects to the unified Mapper hub. */
export default function RetiredSchemaMappingsPage() {
  redirect("/dashboard/mapper");
}
