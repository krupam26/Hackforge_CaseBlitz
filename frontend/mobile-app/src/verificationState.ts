export type BackendVerificationStage = "review" | "success" | "failed";

export function stageForVerificationStatus(status: unknown): BackendVerificationStage {
  if (status === "successful") return "success";
  if (status === "pending" || status === "escalated") return "review";
  return "failed";
}
