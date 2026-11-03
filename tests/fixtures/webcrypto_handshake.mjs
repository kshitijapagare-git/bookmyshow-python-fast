// Write tests/fixtures/webcrypto_handshake.json with Node's Web Crypto, so the
// committed snapshot records an AES-256 key wrapped by a browser-grade
// RSA-OAEP-256 implementation instead of by the Python test suite itself.
//
//   node tests/fixtures/webcrypto_handshake.mjs > tests/fixtures/webcrypto_handshake.json
import { webcrypto } from "node:crypto";

const KID = "2026-10-a";
const ALG = "RSA-OAEP-256";

const subtle = webcrypto.subtle;

const pair = await subtle.generateKey(
  {
    name: "RSA-OAEP",
    modulusLength: 3072,
    publicExponent: new Uint8Array([1, 0, 1]),
    hash: "SHA-256",
  },
  true,
  ["wrapKey", "unwrapKey"],
);

const sessionKey = await subtle.generateKey(
  { name: "AES-GCM", length: 256 },
  true,
  ["encrypt", "decrypt"],
);

const wrapped = await subtle.wrapKey("raw", sessionKey, pair.publicKey, {
  name: "RSA-OAEP",
});

const pkcs8 = Buffer.from(await subtle.exportKey("pkcs8", pair.privateKey));
const jwk = await subtle.exportKey("jwk", pair.publicKey);
const rawKey = Buffer.from(await subtle.exportKey("raw", sessionKey));

const pemBody = pkcs8.toString("base64").match(/.{1,64}/g).join("\n");

const record = {
  kid: KID,
  alg: ALG,
  privateKeyPem: `-----BEGIN PRIVATE KEY-----\n${pemBody}\n-----END PRIVATE KEY-----\n`,
  publicJwk: { kty: jwk.kty, n: jwk.n, e: jwk.e },
  aesKeyB64: rawKey.toString("base64"),
  wrappedKey: Buffer.from(wrapped).toString("base64"),
};

process.stdout.write(JSON.stringify(record, null, 2) + "\n");
