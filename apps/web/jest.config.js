const nextJest = require("next/jest");

const createJestConfig = nextJest({ dir: "./" });

module.exports = createJestConfig({
  testEnvironment: "node",
  // next/jest does not read tsconfig `paths`, so the `@/` alias has to be spelled out or any module
  // that imports through it fails to resolve under test while building fine.
  moduleNameMapper: { "^@/(.*)$": "<rootDir>/$1" },
  testPathIgnorePatterns: ["/node_modules/", "/.next/", "/e2e/"],
});
