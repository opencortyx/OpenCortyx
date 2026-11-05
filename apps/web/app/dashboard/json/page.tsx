import { redirect } from "next/navigation";

/** Retired: JSON files are added in Add data like any file, and what they hold is corrected in the Mapper. */
export default function RetiredJsonPage() {
  redirect("/dashboard/add");
}
