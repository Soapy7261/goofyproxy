import * as crypto from 'crypto';

export function hashPassword(password: string): string {
    const salt = crypto.randomBytes(16).toString('hex');
    const hash = crypto.pbkdf2Sync(password, salt, 1000, 64, 'sha512').toString('hex');
    return `${salt}:${hash}`;
}

export function verifyPassword(password: string, storedHash: string): boolean {
    const [salt, hash] = storedHash.split(':');
    const verifyHash = crypto.pbkdf2Sync(password, salt, 1000, 64, 'sha512').toString('hex');
    return hash === verifyHash;
}

function rc4Crypt(data: Buffer, key: Buffer): Buffer {
    // KSA
    const S = Buffer.alloc(256);
    for (let i = 0; i < 256; i++) S[i] = i;
    let j = 0;
    const keyLen = key.length;
    for (let i = 0; i < 256; i++) {
        j = (j + S[i] + key[i % keyLen]) & 0xFF;
        // swap without temporary variable using destructuring
        [S[i], S[j]] = [S[j], S[i]];
    }

    // PRGA
    const out = Buffer.alloc(data.length);
    let i = 0;
    j = 0;
    for (let idx = 0; idx < data.length; idx++) {
        i = (i + 1) & 0xFF;
        j = (j + S[i]) & 0xFF;
        [S[i], S[j]] = [S[j], S[i]];
        const k = S[(S[i] + S[j]) & 0xFF];
        out[idx] = data[idx] ^ k;
    }
    return out;
}

// NOTE: this is not a secure algorithm and is only used to prevent plain text
// triggers, especially when using non-secure HTTP connections.
export function insecureEncrypt(data: Buffer, key: string): Buffer {
    return rc4Crypt(data, Buffer.from(key, 'utf-8'));
}

// NOTE: this is not a secure algorithm and is only used to prevent plain text
// triggers, especially when using non-secure HTTP connections.
export function insecureDecrypt(data: Buffer, key: string): Buffer {
    return rc4Crypt(data, Buffer.from(key, 'utf-8'));
}
