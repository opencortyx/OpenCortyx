import NextAuth from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";


const apiBase = () => process.env.API_INTERNAL_URL || process.env.NEXT_PUBLIC_API_URL;

/** Seconds since epoch when this JWT expires, or 0 when it cannot be read. */
function expiryOf(accessToken: string): number {
  try {
    const [, payload] = accessToken.split(".");
    const claims = JSON.parse(Buffer.from(payload, "base64").toString("utf8"));
    return Number(claims?.exp) || 0;
  } catch {
    return 0;
  }
}

/** Renew the access token, or mark the session so the app signs the user out cleanly. */
async function refresh(token: any) {
  if (!token.refreshToken) return { ...token, error: "NoRefreshToken" };
  try {
    const res = await fetch(`${apiBase()}/api/v2/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: token.refreshToken }),
    });
    if (!res.ok) return { ...token, error: "RefreshFailed" };
    const data = await res.json();
    return {
      ...token,
      accessToken: data.access_token,
      accessTokenExpires: expiryOf(data.access_token),
      role: data.role ?? token.role,
      workspaceId: data.workspace_id ?? token.workspaceId,
      error: undefined,
    };
  } catch {
    return { ...token, error: "RefreshFailed" };
  }
}

const { handlers } = NextAuth({
  providers: [
    CredentialsProvider({
      name: "Credentials",
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Password", type: "password" },
      },
      async authorize(credentials) {
        if (!credentials?.email || !credentials?.password) return null;

        try {
          // Use Internal URL for server-side fetch inside Docker
          const res = await fetch(
            `${apiBase()}/api/v2/auth/token`,
            {
              method: "POST",
              body: JSON.stringify({
                username: credentials.email as string,
                password: credentials.password as string,
              }),
              headers: { "Content-Type": "application/json" },
            }
          );
          if (!res.ok) return null;
          const data = await res.json();
          return {
            id: "user",
            email: credentials.email as string,
            accessToken: data.access_token,
            refreshToken: data.refresh_token,
            workspaceId: data.workspace_id,
            role: data.role,
          } as any;
        } catch {
          return null;
        }
      },
    }),
  ],
  callbacks: {
    async jwt({ token, user }) {
      if (user) {
        token.accessToken = (user as any).accessToken;
        token.refreshToken = (user as any).refreshToken;
        token.accessTokenExpires = expiryOf((user as any).accessToken || "");
        token.workspaceId = (user as any).workspaceId;
        token.role = (user as any).role;
        token.email = user.email;
        return token;
      }
      // Renew a minute BEFORE expiry, not after: a token that expires while a request is in flight
      // fails that request, and the user sees the failure rather than the renewal.
      const exp = Number((token as any).accessTokenExpires) || 0;
      if (exp && Date.now() / 1000 < exp - 60) return token;
      return refresh(token);
    },
    async session({ session, token }) {
      (session as any).accessToken = token.accessToken;
      (session as any).workspaceId = token.workspaceId;
      (session as any).role = token.role;
      // Surfaced so the client can sign out deliberately instead of silently 401ing forever.
      (session as any).error = (token as any).error;
      if (session.user) {
        session.user.email = token.email as string;
      }
      return session;
    },
  },
  pages: {
    signIn: "/auth/login",
    error: "/auth/login",
  },
  session: { strategy: "jwt" },
  secret: process.env.NEXTAUTH_SECRET,
});

export const { GET, POST } = handlers;
