import { redirect } from "next/navigation";

/** Retired route: opens the mapping in the Mapper. */
export default async function RetiredMappingDetailPage(props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  redirect(`/dashboard/mapper/${encodeURIComponent(params.id)}`);
}
