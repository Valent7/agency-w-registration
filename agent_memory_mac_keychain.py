"""macOS generic-password item via Security.framework. No secret in argv or files."""
import ctypes as C
import sys


class Keychain:
    def __init__(self, service='agency-w.memory-sync'):
        if sys.platform != 'darwin': raise RuntimeError('macOS Keychain required')
        self.service=service
        self.cf=C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.sec=C.CDLL('/System/Library/Frameworks/Security.framework/Security')
        self.cf.CFStringCreateWithCString.argtypes=[C.c_void_p,C.c_char_p,C.c_uint32];self.cf.CFStringCreateWithCString.restype=C.c_void_p
        self.cf.CFDataCreate.argtypes=[C.c_void_p,C.c_void_p,C.c_long];self.cf.CFDataCreate.restype=C.c_void_p
        self.cf.CFDataGetLength.argtypes=[C.c_void_p];self.cf.CFDataGetLength.restype=C.c_long
        self.cf.CFDataGetBytePtr.argtypes=[C.c_void_p];self.cf.CFDataGetBytePtr.restype=C.c_void_p
        self.cf.CFRelease.argtypes=[C.c_void_p]
        self.cf.CFDictionaryCreate.argtypes=[C.c_void_p,C.POINTER(C.c_void_p),C.POINTER(C.c_void_p),C.c_long,C.c_void_p,C.c_void_p];self.cf.CFDictionaryCreate.restype=C.c_void_p
        for name in ['SecItemAdd','SecItemCopyMatching']:
            getattr(self.sec,name).argtypes=[C.c_void_p,C.POINTER(C.c_void_p)];getattr(self.sec,name).restype=C.c_int32

    def const(self,name,lib=None):
        return C.c_void_p.in_dll(lib or self.sec,name).value

    def string(self,value):
        return self.cf.CFStringCreateWithCString(None,value.encode(),0x08000100)

    def dictionary(self,pairs):
        keys=(C.c_void_p*len(pairs))(*[k for k,v in pairs]); vals=(C.c_void_p*len(pairs))(*[v for k,v in pairs])
        return self.cf.CFDictionaryCreate(None,keys,vals,len(pairs),
            C.addressof(C.c_byte.in_dll(self.cf,'kCFTypeDictionaryKeyCallBacks')),
            C.addressof(C.c_byte.in_dll(self.cf,'kCFTypeDictionaryValueCallBacks')))

    def item(self,account,secret=None):
        service=self.string(self.service);user=self.string(account);owned=[service,user]
        pairs=[(self.const('kSecClass'),self.const('kSecClassGenericPassword')),
               (self.const('kSecAttrService'),service),(self.const('kSecAttrAccount'),user)]
        if secret is None:
            pairs += [(self.const('kSecReturnData'),self.const('kCFBooleanTrue',self.cf)),
                      (self.const('kSecMatchLimit'),self.const('kSecMatchLimitOne')),
                      (self.const('kSecUseAuthenticationUI'),self.const('kSecUseAuthenticationUIFail'))]
        else:
            buf=C.create_string_buffer(secret);data=self.cf.CFDataCreate(None,buf,len(secret));owned.append(data)
            pairs += [(self.const('kSecValueData'),data),
                      (self.const('kSecAttrAccessible'),self.const('kSecAttrAccessibleWhenUnlockedThisDeviceOnly'))]
        query=self.dictionary(pairs)
        for value in owned:self.cf.CFRelease(value)
        return query

    def get(self,account):
        query=self.item(account);result=C.c_void_p()
        try:
            status=self.sec.SecItemCopyMatching(query,C.byref(result))
            if status: raise RuntimeError('Keychain unavailable or locked')
            return C.string_at(self.cf.CFDataGetBytePtr(result),self.cf.CFDataGetLength(result))
        finally:
            self.cf.CFRelease(query)
            if result.value:self.cf.CFRelease(result)

    def create(self,account,secret):
        query=self.item(account,secret)
        try:
            if self.sec.SecItemAdd(query,None):raise RuntimeError('Keychain item not created; existing credentials never overwritten')
        finally:self.cf.CFRelease(query)
