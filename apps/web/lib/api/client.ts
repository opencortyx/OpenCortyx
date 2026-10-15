import axios from "axios";
import { getSession } from "next-auth/react";

export const apiClient = axios.create({
  baseURL: process.env.NEXT_PUBLIC_API_URL,
  headers: { "Content-Type": "application/json" },
});

// Attach JWT from next-auth session to every request
apiClient.interceptors.request.use(async (config) => {
  try {
    const session = await getSession() as any;
    if (session?.accessToken) {
      config.headers.Authorization = `Bearer ${session.accessToken}`;
    }
  } catch {
    // No session available (e.g. during SSR)
  }
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (error.response?.status === 401 && typeof window !== "undefined") {
      window.location.href = "/auth/login";
    }
    return Promise.reject(error);
  }
);

// Server-side client that accepts an explicit token (for Server Components)
export function createServerClient(accessToken: string) {
  return axios.create({
    baseURL: process.env.NEXT_PUBLIC_API_URL,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
  });
}
