import { CONFIG } from '../config';

export function validateUserId(userId: string): boolean {
    if (!userId || userId.length === 0) {
        return false;
    }

    if (userId.length > CONFIG.MAX_USERNAME_LENGTH) {
        return false;
    }

    // Check allowed characters: a-Z, 0-9, dash, underscore
    const allowedPattern = /^[a-zA-Z0-9_-]+$/;
    if (!allowedPattern.test(userId)) {
        return false;
    }

    // First and last characters cannot be dash or underscore
    if (userId.startsWith('-') || userId.startsWith('_') ||
        userId.endsWith('-') || userId.endsWith('_')) {
        return false;
    }

    return true;
}

export function validatePassword(password: string): boolean {
    if (!password || password.length === 0) {
        return false;
    }

    if (password.length < CONFIG.MIN_PASSWORD_LENGTH ||
        password.length > CONFIG.MAX_PASSWORD_LENGTH) {
        return false;
    }

    // Check for more than 3 adjacent identical characters
    let maxAdjacent = 1;
    let currentAdjacent = 1;

    for (let i = 1; i < password.length; i++) {
        if (password[i] === password[i - 1]) {
            currentAdjacent++;
            if (currentAdjacent > maxAdjacent) {
                maxAdjacent = currentAdjacent;
            }
        } else {
            currentAdjacent = 1;
        }
    }

    if (maxAdjacent > CONFIG.MAX_ADJACENT_IDENTICAL_CHARS) {
        return false;
    }

    return true;
}
