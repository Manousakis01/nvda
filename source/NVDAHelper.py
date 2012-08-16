import os
import sys
import _winreg
import msvcrt
import winKernel
import config

from ctypes import *
from ctypes.wintypes import *
from comtypes import BSTR
import winUser
import eventHandler
import queueHandler
import api
import globalVars
from logHandler import log
import time
import globalVars

_remoteLib=None
_remoteLoader64=None
localLib=None
generateBeep=None
VBuf_getTextInRange=None
lastInputMethodName=None

#utility function to point an exported function pointer in a dll  to a ctypes wrapped python function
def _setDllFuncPointer(dll,name,cfunc):
	cast(getattr(dll,name),POINTER(c_void_p)).contents.value=cast(cfunc,c_void_p).value

#Implementation of nvdaController methods
@WINFUNCTYPE(c_long,c_wchar_p)
def nvdaController_speakText(text):
	focus=api.getFocusObject()
	if focus.sleepMode==focus.SLEEP_FULL:
		return -1
	import queueHandler
	import speech
	queueHandler.queueFunction(queueHandler.eventQueue,speech.speakText,text)
	return 0

@WINFUNCTYPE(c_long)
def nvdaController_cancelSpeech():
	focus=api.getFocusObject()
	if focus.sleepMode==focus.SLEEP_FULL:
		return -1
	import queueHandler
	import speech
	queueHandler.queueFunction(queueHandler.eventQueue,speech.cancelSpeech)
	return 0

@WINFUNCTYPE(c_long,c_wchar_p)
def nvdaController_brailleMessage(text):
	focus=api.getFocusObject()
	if focus.sleepMode==focus.SLEEP_FULL:
		return -1
	import queueHandler
	import braille
	queueHandler.queueFunction(queueHandler.eventQueue,braille.handler.message,text)
	return 0

def _lookupKeyboardLayoutNameWithHexString(layoutString):
	buf=create_unicode_buffer(1024)
	bufSize=c_int(2048)
	key=HKEY()
	if windll.advapi32.RegOpenKeyExW(_winreg.HKEY_LOCAL_MACHINE,u"SYSTEM\\CurrentControlSet\\Control\\Keyboard Layouts\\"+ layoutString,0,_winreg.KEY_QUERY_VALUE,byref(key))==0:
		try:
			if windll.advapi32.RegQueryValueExW(key,u"Layout Display Name",0,None,buf,byref(bufSize))==0:
				windll.shlwapi.SHLoadIndirectString(buf.value,buf,1023,None)
				return buf.value
			if windll.advapi32.RegQueryValueExW(key,u"Layout Text",0,None,buf,byref(bufSize))==0:
				return buf.value
		finally:
			windll.advapi32.RegCloseKey(key)

@WINFUNCTYPE(c_long,c_wchar_p)
def nvdaControllerInternal_requestRegistration(uuidString):
	pid=c_long()
	windll.rpcrt4.I_RpcBindingInqLocalClientPID(None,byref(pid))
	pid=pid.value
	if not pid:
		log.error("Could not get process ID for RPC call")
		return -1;
	bindingHandle=c_long()
	bindingHandle.value=localLib.createRemoteBindingHandle(uuidString)
	if not bindingHandle: 
		log.error("Could not bind to inproc rpc server for pid %d"%pid)
		return -1
	registrationHandle=c_long()
	res=localLib.nvdaInProcUtils_registerNVDAProcess(bindingHandle,byref(registrationHandle))
	if res!=0 or not registrationHandle:
		log.error("Could not register NVDA with inproc rpc server for pid %d, res %d, registrationHandle %s"%(pid,res,registrationHandle))
		windll.rpcrt4.RpcBindingFree(byref(bindingHandle))
		return -1
	import appModuleHandler
	queueHandler.queueFunction(queueHandler.eventQueue,appModuleHandler.update,pid,helperLocalBindingHandle=bindingHandle,inprocRegistrationHandle=registrationHandle)
	return 0

@WINFUNCTYPE(c_long,c_long,c_long,c_long,c_long,c_long)
def nvdaControllerInternal_displayModelTextChangeNotify(hwnd, left, top, right, bottom):
	import displayModel
	displayModel.textChangeNotify(hwnd, left, top, right, bottom)
	return 0

@WINFUNCTYPE(c_long,c_long,c_long,c_wchar_p)
def nvdaControllerInternal_logMessage(level,pid,message):
	if not log.isEnabledFor(level):
		return 0
	if pid:
		from appModuleHandler import getAppNameFromProcessID
		codepath="RPC process %s (%s)"%(pid,getAppNameFromProcessID(pid,includeExt=True))
	else:
		codepath="NVDAHelperLocal"
	log._log(level,message,[],codepath=codepath)
	return 0

def handleInputCompositionEnd(result):
	import speech
	import characterProcessing
	from NVDAObjects.inputComposition import InputComposition
	from NVDAObjects.behaviors import CandidateItem
	focus=api.getFocusObject()
	result=result.lstrip(u'\u3000 ')
	curInputComposition=None
	if isinstance(focus,InputComposition):
		curInputComposition=focus
		oldSpeechMode=speech.speechMode
		speech.speechMode=speech.speechMode_off
		eventHandler.executeEvent("gainFocus",focus.parent)
		speech.speechMode=oldSpeechMode
	elif isinstance(focus.parent,InputComposition):
		#Candidate list is still up
		curInputComposition=focus.parent
		focus.parent=focus.parent.parent
	if curInputComposition and not result:
		result=curInputComposition.compositionString.lstrip(u'\u3000 ')
	if result:
		speech.speakText(result,symbolLevel=characterProcessing.SYMLVL_ALL)

def handleInputCompositionStart(compositionString,selectionStart,selectionEnd,isReading):
	import speech
	from NVDAObjects.inputComposition import InputComposition
	from NVDAObjects.behaviors import CandidateItem
	focus=api.getFocusObject()
	#IME keeps updating input composition while the candidate list is open
	#Therefore ignore composition updates in this situation.
	if isinstance(focus,CandidateItem):
		return 0
	if not isinstance(focus,InputComposition):
		parent=api.getDesktopObject().objectWithFocus()
		curInputComposition=InputComposition(parent=parent)
		oldSpeechMode=speech.speechMode
		speech.speechMode=speech.speechMode_off
		eventHandler.executeEvent("gainFocus",curInputComposition)
		focus=curInputComposition
		speech.speechMode=oldSpeechMode
	focus.compositionUpdate(compositionString,selectionStart,selectionEnd,isReading)

@WINFUNCTYPE(c_long,c_wchar_p,c_int,c_int,c_int)
def nvdaControllerInternal_inputCompositionUpdate(compositionString,selectionStart,selectionEnd,isReading):
	from NVDAObjects.inputComposition import InputComposition
	if selectionStart==-1:
		queueHandler.queueFunction(queueHandler.eventQueue,handleInputCompositionEnd,compositionString)
		return 0
	focus=api.getFocusObject()
	if isinstance(focus,InputComposition):
		focus.compositionUpdate(compositionString,selectionStart,selectionEnd,isReading)
	else:
		queueHandler.queueFunction(queueHandler.eventQueue,handleInputCompositionStart,compositionString,selectionStart,selectionEnd,isReading)
	return 0

def handleInputCandidateListUpdate(candidatesString,selectionIndex):
	candidateStrings=candidatesString.split('\n')
	import speech
	from NVDAObjects.inputComposition import InputComposition, CandidateList, CandidateItem
	focus=api.getFocusObject()
	if not (0<=selectionIndex<len(candidateStrings)):
		if isinstance(focus,CandidateItem):
			oldSpeechMode=speech.speechMode
			speech.speechMode=speech.speechMode_off
			eventHandler.executeEvent("gainFocus",focus.parent)
			speech.speechMode=oldSpeechMode
		return
	oldCandidateItemsText=None
	if isinstance(focus,CandidateItem):
		oldCandidateItemsText=focus.visibleCandidateItemsText
		parent=focus.parent
		wasCandidate=True
	else:
		parent=focus
		wasCandidate=False
	item=CandidateItem(parent=parent,candidateStrings=candidateStrings,candidateIndex=selectionIndex)
	if wasCandidate and focus.windowHandle==item.windowHandle and focus.candidateIndex==item.candidateIndex and focus.name==item.name:
		return
	if item.visibleCandidateItemsText!=oldCandidateItemsText:
		import ui
		ui.message(item.visibleCandidateItemsText)
	eventHandler.executeEvent("gainFocus",item)

@WINFUNCTYPE(c_long,c_wchar_p,c_long)
def nvdaControllerInternal_inputCandidateListUpdate(candidatesString,selectionIndex):
	queueHandler.queueFunction(queueHandler.eventQueue,handleInputCandidateListUpdate,candidatesString,selectionIndex)
	return 0

inputConversionModeMessages={
	1:(_("Native input"),_("Alpha numeric input")),
	8:(_("Full shaped mode"),_("Half shaped mode")),
}

def handleInputConversionModeUpdate(oldFlags,newFlags):
	import speech
	for x in xrange(32):
		x=2**x
		msgs=inputConversionModeMessages.get(x)
		if not msgs: continue
		newOn=bool(newFlags&x)
		oldOn=bool(oldFlags&x)
		if newOn!=oldOn: 
			queueHandler.queueFunction(queueHandler.eventQueue,speech.speakMessage,msgs[0] if newOn else msgs[1])

@WINFUNCTYPE(c_long,c_long,c_long)
def nvdaControllerInternal_inputConversionModeUpdate(oldFlags,newFlags):
	queueHandler.queueFunction(queueHandler.eventQueue,handleInputConversionModeUpdate,oldFlags,newFlags)
	return 0

@WINFUNCTYPE(c_long,c_long,c_ulong,c_wchar_p)
def nvdaControllerInternal_inputLangChangeNotify(threadID,hkl,layoutString):
	global lastInputMethodName
	if api.getFocusObject().sleepMode:
		return 0
	import queueHandler
	import ui
	print "inputLangChange: layoutString %s"%layoutString
	layoutStringCodes=[]
	inputMethodName=None
	#layoutString can either be a real input method name, a hex string for an input method name in the registry, or an empty string.
	#If its a real input method name its used as is.
	#If its a hex string or its empty, then the method name is looked up by trying:
	#The full hex string, the hkl as a hex string, the low word of the hex string or hkl, the high word of the hex string or hkl.
	if layoutString:
		try:
			int(layoutString,16)
			layoutStringCodes.append(layoutString)
		except ValueError:
			print "already input method name"
			inputMethodName=layoutString
			pass
	if not inputMethodName:
		layoutStringCodes.insert(0,hex(hkl)[2:].rstrip('L').upper().rjust(8,'0'))
		for stringCode in list(layoutStringCodes):
			layoutStringCodes.append(stringCode[4:].rjust(8,'0'))
			if stringCode[0]<'D':
				layoutStringCodes.append(stringCode[0:4].rjust(8,'0'))
		for stringCode in layoutStringCodes:
			inputMethodName=_lookupKeyboardLayoutNameWithHexString(stringCode)
			print "stringCode %s, input method name %s"%(stringCode,inputMethodName)
			if inputMethodName: break
	if not inputMethodName:
		log.debugWarning("Could not find layout name for keyboard layout, reporting as unknown") 
		inputMethodName=_("unknown input method")
	if inputMethodName!=lastInputMethodName:
		lastInputMethodName=inputMethodName
		queueHandler.queueFunction(queueHandler.eventQueue,ui.message,inputMethodName)
	return 0

@WINFUNCTYPE(c_long,c_long,c_wchar)
def nvdaControllerInternal_typedCharacterNotify(threadID,ch):
	focus=api.getFocusObject()
	if focus.windowClassName!="ConsoleWindowClass":
		eventHandler.queueEvent("typedCharacter",focus,ch=ch)
	return 0

@WINFUNCTYPE(c_long, c_int, c_int)
def nvdaControllerInternal_vbufChangeNotify(rootDocHandle, rootID):
	import virtualBuffers
	virtualBuffers.VirtualBuffer.changeNotify(rootDocHandle, rootID)
	return 0

class RemoteLoader64(object):

	def __init__(self):
		# Create a pipe so we can write to stdin of the loader process.
		pipeReadOrig, self._pipeWrite = winKernel.CreatePipe(None, 0)
		# Make the read end of the pipe inheritable.
		pipeRead = self._duplicateAsInheritable(pipeReadOrig)
		winKernel.closeHandle(pipeReadOrig)
		# stdout/stderr of the loader process should go to nul.
		with file("nul", "w") as nul:
			nulHandle = self._duplicateAsInheritable(msvcrt.get_osfhandle(nul.fileno()))
		# Set the process to start with the appropriate std* handles.
		si = winKernel.STARTUPINFO(dwFlags=winKernel.STARTF_USESTDHANDLES, hSTDInput=pipeRead, hSTDOutput=nulHandle, hSTDError=nulHandle)
		pi = winKernel.PROCESS_INFORMATION()
		# Even if we have uiAccess privileges, they will not be inherited by default.
		# Therefore, explicitly specify our own process token, which causes them to be inherited.
		token = winKernel.OpenProcessToken(winKernel.GetCurrentProcess(), winKernel.MAXIMUM_ALLOWED)
		try:
			winKernel.CreateProcessAsUser(token, None, u"lib64/nvdaHelperRemoteLoader.exe", None, None, True, None, None, None, si, pi)
			# We don't need the thread handle.
			winKernel.closeHandle(pi.hThread)
			self._process = pi.hProcess
		except:
			winKernel.closeHandle(self._pipeWrite)
			raise
		finally:
			winKernel.closeHandle(pipeRead)
			winKernel.closeHandle(token)

	def _duplicateAsInheritable(self, handle):
		curProc = winKernel.GetCurrentProcess()
		return winKernel.DuplicateHandle(curProc, handle, curProc, 0, True, winKernel.DUPLICATE_SAME_ACCESS)

	def terminate(self):
		# Closing the write end of the pipe will cause EOF for the waiting loader process, which will then exit gracefully.
		winKernel.closeHandle(self._pipeWrite)
		# Wait until it's dead.
		winKernel.waitForSingleObject(self._process, winKernel.INFINITE)
		winKernel.closeHandle(self._process)

def initialize():
	global _remoteLib, _remoteLoader64, localLib, generateBeep,VBuf_getTextInRange
	localLib=cdll.LoadLibrary('lib/nvdaHelperLocal.dll')
	for name,func in [
		("nvdaController_speakText",nvdaController_speakText),
		("nvdaController_cancelSpeech",nvdaController_cancelSpeech),
		("nvdaController_brailleMessage",nvdaController_brailleMessage),
		("nvdaControllerInternal_requestRegistration",nvdaControllerInternal_requestRegistration),
		("nvdaControllerInternal_inputLangChangeNotify",nvdaControllerInternal_inputLangChangeNotify),
		("nvdaControllerInternal_typedCharacterNotify",nvdaControllerInternal_typedCharacterNotify),
		("nvdaControllerInternal_displayModelTextChangeNotify",nvdaControllerInternal_displayModelTextChangeNotify),
		("nvdaControllerInternal_logMessage",nvdaControllerInternal_logMessage),
		("nvdaControllerInternal_inputCompositionUpdate",nvdaControllerInternal_inputCompositionUpdate),
		("nvdaControllerInternal_inputCandidateListUpdate",nvdaControllerInternal_inputCandidateListUpdate),
		("nvdaControllerInternal_inputConversionModeUpdate",nvdaControllerInternal_inputConversionModeUpdate),
		("nvdaControllerInternal_vbufChangeNotify",nvdaControllerInternal_vbufChangeNotify),
	]:
		try:
			_setDllFuncPointer(localLib,"_%s"%name,func)
		except AttributeError as e:
			log.error("nvdaHelperLocal function pointer for %s could not be found, possibly old nvdaHelperLocal dll"%name,exc_info=True)
			raise e
	localLib.nvdaHelperLocal_initialize()
	generateBeep=localLib.generateBeep
	generateBeep.argtypes=[c_char_p,c_float,c_uint,c_ubyte,c_ubyte]
	generateBeep.restype=c_uint
	# Handle VBuf_getTextInRange's BSTR out parameter so that the BSTR will be freed automatically.
	VBuf_getTextInRange = CFUNCTYPE(c_int, c_int, c_int, c_int, POINTER(BSTR), c_int)(
		("VBuf_getTextInRange", localLib),
		((1,), (1,), (1,), (2,), (1,)))
	#Load nvdaHelperRemote.dll but with an altered search path so it can pick up other dlls in lib
	h=windll.kernel32.LoadLibraryExW(os.path.abspath(ur"lib\nvdaHelperRemote.dll"),0,0x8)
	if not h:
		log.critical("Error loading nvdaHelperRemote.dll: %s" % WinError())
		return
	_remoteLib=CDLL("nvdaHelperRemote",handle=h)
	if _remoteLib.injection_initialize(globalVars.appArgs.secure) == 0:
		raise RuntimeError("Error initializing NVDAHelperRemote")
	if not _remoteLib.installIA2Support():
		log.error("Error installing IA2 support")
	#Manually start the in-process manager thread for this NVDA main thread now, as a slow system can cause this action to confuse WX
	_remoteLib.initInprocManagerThreadIfNeeded()
	if os.environ.get('PROCESSOR_ARCHITEW6432')=='AMD64':
		_remoteLoader64=RemoteLoader64()

def terminate():
	global _remoteLib, _remoteLoader64, localLib, generateBeep, VBuf_getTextInRange
	if not _remoteLib.uninstallIA2Support():
		log.debugWarning("Error uninstalling IA2 support")
	if _remoteLib.injection_terminate() == 0:
		raise RuntimeError("Error terminating NVDAHelperRemote")
	_remoteLib=None
	if _remoteLoader64:
		_remoteLoader64.terminate()
		_remoteLoader64=None
	generateBeep=None
	VBuf_getTextInRange=None
	localLib.nvdaHelperLocal_terminate()
	localLib=None
