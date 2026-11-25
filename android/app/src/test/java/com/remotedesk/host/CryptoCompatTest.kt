package com.remotedesk.host

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test
import java.math.BigInteger
import java.security.KeyFactory
import java.security.KeyPair
import java.security.interfaces.ECPublicKey
import java.security.spec.ECPoint
import java.security.spec.ECPrivateKeySpec
import java.security.spec.ECPublicKeySpec

/** Vectors produced by client/core.py - the phone must interoperate byte-for-byte. */
class CryptoCompatTest {
    private val hn = "aa11"
    private val vn = "bb22"
    private val key = Crypto.deriveKey("pass-1234 שלום", "123456789")

    @Test fun pbkdf2() = assertEquals("1874fc60298a93be63c26ff31dbaafca44787049f12421dca7b8a1e7b9f8342b", Crypto.hex(key))

    @Test fun proof() = assertEquals("29847526f392a01f0c598d206160464f3368804171f7563a51762088eda6a637",
        Crypto.proof(key, "viewer", hn, vn))

    @Test fun sessionKey() = assertEquals("23f322b32373571c237d83956c5b1bfb731092f6d446d89c9a18ca195822bdbe",
        Crypto.hex(Crypto.sessionKey(key, hn, vn)))

    @Test fun code() = assertEquals("584 817", Crypto.verificationCode(key))

    @Test fun decryptFromPython() {
        val ch = SecureChannel({ true }, Crypto.sessionKey(key, hn, vn))
        val r = ch.decrypt(Crypto.unhex("020000000000000000000001afd11add509e970972ab4fe0c76baf375c046dd50de7e68613b0d1fee2eb90"))
        assertNotNull(r)
        assertEquals(0, r!!.first)
        assertEquals("{\"type\":\"ack\"}", String(r.second))
    }

    @Test fun ecdh() {
        val params = (Crypto.ecdhKeyPair().public as ECPublicKey).params
        val kf = KeyFactory.getInstance("EC")
        val pubHex = "04bd7c73b88b2e9b4ceda62022b2da8be13193a5b56edc26e7df7842e24cd0b5eb0605ada7bda83ac6a2b80d7e314040fa47ff16b83bac85cedb014451bb7ce71a"
        val p = Crypto.unhex(pubHex)
        val pub = kf.generatePublic(ECPublicKeySpec(ECPoint(BigInteger(1, p.copyOfRange(1, 33)), BigInteger(1, p.copyOfRange(33, 65))), params))
        val priv = kf.generatePrivate(ECPrivateKeySpec(BigInteger("1f2e3d4c5b6a79881f2e3d4c5b6a79881f2e3d4c5b6a79881f2e3d4c5b6a7988", 16), params))
        val kp = KeyPair(pub, priv)
        assertEquals(pubHex, Crypto.hex(Crypto.encodePub(kp)))
        val peer = "04160b0615159ebbdf7fd4b1194d42b2986c6b2cccad799521b8b326292b6419b393a20e124ad2a56ecea74f8dae84d8d91d0f5ac4a33f221226d1681cd12552ca"
        assertEquals("db28e5e7ac03199cbebb5b67d9f4bb2cfe3011907a3a3db2c35ef94f089e5e7b", Crypto.hex(Crypto.ecdhKey(kp, peer, hn, vn)))
    }
}
