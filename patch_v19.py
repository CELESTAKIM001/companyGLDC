from pathlib import Path
p=Path('/mnt/data/gldc_v19/app.py')
s=p.read_text()
# Enhance admin members output with latest payment info
old="""def admin_members_v14():\n    xs=list(db.members.find({}).sort('createdAt',DESCENDING).limit(500)); out=[]\n    for x in xs: out.append(clean_doc(sync_membership_state(x)))\n    return jsonify(ok=True,members=out)\n"""
new="""def admin_members_v14():\n    xs=list(db.members.find({}).sort('createdAt',DESCENDING).limit(500)); out=[]\n    for x in xs:\n        x=sync_membership_state(x)\n        latest=db.payments.find_one({'memberId':x.get('id')},sort=[('createdAt',DESCENDING)])\n        if latest:\n            x['latestPaymentStatus']=latest.get('status')\n            x['latestPaymentId']=latest.get('id')\n            x['latestMpesaReceiptNumber']=latest.get('mpesaReceiptNumber')\n            x['latestPaymentAmount']=latest.get('amount')\n            x['latestPaymentReference']=latest.get('reference')\n            x['paymentReceiptCode']=latest.get('receiptCode') or x.get('paymentReceiptCode')\n        out.append(clean_doc(x))\n    return jsonify(ok=True,members=out)\n"""
assert old in s
s=s.replace(old,new)
# Insert manual payment + recreate routes before admin membership plans
marker="@app.get('/api/admin/membership/plans')\n"
insert=r'''@app.post('/api/membership/submit-payment-reference')
@login_required
def membership_submit_payment_reference():
    """Let a member whose STK prompt failed but who actually paid submit the M-Pesa receipt for admin verification.
    This NEVER marks the payment successful automatically; an admin must verify it first.
    """
    try:
        m=_member_doc()
        if not m: return json_error('Member application not found.',404,'MEMBER_NOT_FOUND')
        b=request.get_json(silent=True) or {}
        code=re.sub(r'[^A-Za-z0-9]','',str(b.get('mpesaCode','')).upper()).strip()
        if not re.fullmatch(r'[A-Z0-9]{8,20}',code): return json_error('Enter the M-Pesa transaction code exactly as shown on your M-Pesa message.',422,'MPESA_CODE_INVALID')
        try: amount=float(b.get('amount',0) or 0)
        except Exception: amount=0
        phone_input=str(b.get('phone','')).strip()
        try: phone=normalize_mpesa_phone(phone_input or m.get('phone',''))
        except RuntimeError: return json_error('Please enter a valid Kenyan M-Pesa number.',422,'PHONE_INVALID')
        payment_date=str(b.get('paymentDate','')).strip()
        if amount<=0: return json_error('Enter the amount you paid.',422,'AMOUNT_INVALID')
        latest=db.payments.find_one({'memberId':m['id']},sort=[('createdAt',DESCENDING)])
        if not latest: return json_error('No membership payment attempt was found. Please select your membership plan first.',409,'PAYMENT_NOT_FOUND')
        if latest.get('status')=='SUCCESSFUL': return json_error('Your payment is already recorded. Please wait for GLDC validation.',409,'PAYMENT_ALREADY_RECORDED')
        plan=db.membership_plans.find_one({'id':latest.get('planId')})
        if plan and abs(float(plan.get('price',0))-amount)>0.01:
            return json_error(f'The amount does not match the selected plan ({CURRENCY} {float(plan.get("price",0)):,.2f}). Please check your M-Pesa message.',422,'AMOUNT_MISMATCH')
        duplicate=db.payments.find_one({'mpesaReceiptNumber':code})
        if duplicate and duplicate.get('memberId')!=m.get('id'):
            return json_error('That M-Pesa transaction code has already been submitted for another application.',409,'MPESA_CODE_USED')
        update={'status':'PENDING_ADMIN_VERIFICATION','mpesaReceiptNumber':code,'reference':code,'manualPaymentCode':code,'manualAmount':amount,'phoneNumber':phone,'manualPaymentDate':payment_date or None,'manualSubmittedAt':now(),'updatedAt':now(),'verificationNote':'Member submitted M-Pesa receipt for admin verification.'}
        db.payments.update_one({'_id':latest['_id']},{'$set':update})
        db.members.update_one({'_id':m['_id']},{'$set':{'status':'PENDING_REVIEW','paymentId':latest.get('id'),'paymentReceiptCode':latest.get('receiptCode'),'updatedAt':now()}})
        audit('MEMBERSHIP_MANUAL_PAYMENT_SUBMITTED','member',m['id'],{'paymentId':latest.get('id'),'mpesaCode':code})
        try:
            db.notifications.insert_one({'id':make_id('NOT'),'memberId':m['id'],'title':'Payment submitted for GLDC verification','message':'Your M-Pesa receipt has been submitted. GLDC will verify the transaction before approving your membership.','type':'MEMBERSHIP','audience':'MEMBER','createdAt':now()})
        except Exception: pass
        return jsonify(ok=True,status='PENDING_REVIEW',message='Payment details received. GLDC will verify the M-Pesa transaction and review your membership application.')
    except Exception:
        app.logger.exception('Manual membership payment submission failed request=%s',getattr(request,'request_id','unknown'))
        return json_error('We could not submit your payment details right now. Please try again.',503,'PAYMENT_REFERENCE_UNAVAILABLE')

@app.get('/membership/recreate/<token>')
def membership_recreate_page(token):
    m=db.members.find_one({'recreateToken':token}) if db is not None else None
    if not m or not token: return render_template('404.html',title='Link expired'),404
    allowed={'EMAIL_PENDING','PENDING_PAYMENT','PAYMENT_FAILED','PAYMENT_PENDING','CHANGES_REQUIRED','REJECTED'}
    if str(m.get('status','')).upper() not in allowed: return render_template('membership_recreate.html',title='Cannot restart membership',allowed=False,message='This membership application can no longer be removed because payment or membership processing has progressed.'),409
    paid=db.payments.find_one({'memberId':m.get('id'),'status':{'$in':['SUCCESSFUL','PENDING_ADMIN_VERIFICATION']}})
    if paid: return render_template('membership_recreate.html',title='Cannot restart membership',allowed=False,message='This application has a payment record under review. Please wait for GLDC validation instead of creating another application.'),409
    return render_template('membership_recreate.html',title='Restart membership application',allowed=True,member=clean_doc(m),token=token)

@app.post('/api/membership/recreate/<token>')
def membership_recreate(token):
    try:
        m=db.members.find_one({'recreateToken':token}) if db is not None else None
        if not m: return json_error('This restart link is invalid or has expired.',404,'RECREATE_NOT_FOUND')
        allowed={'EMAIL_PENDING','PENDING_PAYMENT','PAYMENT_FAILED','PAYMENT_PENDING','CHANGES_REQUIRED','REJECTED'}
        if str(m.get('status','')).upper() not in allowed: return json_error('This application cannot be removed because membership processing has progressed.',409,'RECREATE_BLOCKED')
        paid=db.payments.find_one({'memberId':m.get('id'),'status':{'$in':['SUCCESSFUL','PENDING_ADMIN_VERIFICATION']}})
        if paid: return json_error('A payment is already under review. Please wait for GLDC validation.',409,'PAYMENT_UNDER_REVIEW')
        db.otps.delete_many({'email':m.get('email')})
        db.membership_renewals.delete_many({'memberId':m.get('id')})
        db.payments.delete_many({'memberId':m.get('id')})
        db.member_documents.delete_many({'memberId':m.get('id')})
        db.notifications.delete_many({'memberId':m.get('id')})
        db.members.delete_one({'_id':m['_id']})
        audit('MEMBERSHIP_APPLICATION_REMOVED','member',m.get('id'),{'email':m.get('email')})
        session.pop('user',None)
        return jsonify(ok=True,message='Your unfinished membership application has been removed. You can now create a new membership application.')
    except Exception:
        app.logger.exception('Membership recreate failed')
        return json_error('We could not remove the unfinished application right now. Please try again.',503,'RECREATE_FAILED')

@app.post('/api/admin/membership/payments/<payment_id>/verify')
@admin_required
def admin_verify_membership_payment(payment_id):
    b=request.get_json(silent=True) or {}; decision=str(b.get('decision','VERIFY')).upper(); p=db.payments.find_one({'id':payment_id})
    if not p: return json_error('Payment not found.',404,'PAYMENT_NOT_FOUND')
    if decision not in {'VERIFY','REJECT'}: return json_error('Invalid payment decision.',422,'VALIDATION_ERROR')
    m=db.members.find_one({'id':p.get('memberId')})
    if not m: return json_error('Member application not found.',404,'MEMBER_NOT_FOUND')
    if decision=='VERIFY':
        code=str(p.get('mpesaReceiptNumber') or p.get('manualPaymentCode') or '').strip().upper()
        if not code: return json_error('No M-Pesa receipt code has been submitted.',409,'MPESA_CODE_MISSING')
        db.payments.update_one({'_id':p['_id']},{'$set':{'status':'SUCCESSFUL','verifiedManually':True,'verifiedAt':now(),'verifiedBy':current_user().get('email'),'resultDescription':'Verified by GLDC administrator.','updatedAt':now(),'mpesaReceiptNumber':code}})
        db.members.update_one({'_id':m['_id']},{'$set':{'status':'PENDING_REVIEW','paymentId':p['id'],'paymentReceiptCode':p.get('receiptCode'),'updatedAt':now()}})
        db.notifications.insert_one({'id':make_id('NOT'),'memberId':m['id'],'title':'Payment verified by GLDC','message':'Your M-Pesa payment has been verified. Your membership application is now awaiting final GLDC approval.','type':'MEMBERSHIP','audience':'MEMBER','createdAt':now()})
        try: _send_member_email('membership_payment_verified',m,{'name':m.get('name',''),'plan':p.get('planName',''),'amount':p.get('amount'),'mpesaCode':code,'subject':'GLDC Membership Payment Verified','text':f'Your M-Pesa payment {code} has been verified. Your application is now awaiting final GLDC approval.','html':f'<div style="font-family:Arial;max-width:640px;margin:auto"><h2 style="color:#8B4A18">Payment verified ✓</h2><p>Dear {m.get("name","")},</p><p>GLDC has verified your M-Pesa payment for <b>{p.get("planName","")}</b>.</p><p><b>M-Pesa code:</b> {code}<br><b>Amount:</b> KES {float(p.get("amount",0)):,.2f}</p><p>Your membership application is now awaiting final GLDC approval. Please do not make another payment.</p><p><a href="{APP_URL}/member/dashboard">OPEN MEMBER PORTAL</a></p></div>'})
        except Exception: app.logger.exception('Payment verified email failed')
        audit('MEMBERSHIP_PAYMENT_VERIFIED','payment',payment_id,{'memberId':m['id'],'mpesaCode':code})
        return jsonify(ok=True,status='SUCCESSFUL',message='Payment verified. The membership application is now ready for final approval.')
    note=str(b.get('message','The M-Pesa receipt could not be verified. Please check the transaction details and submit the correct code.')).strip()
    db.payments.update_one({'_id':p['_id']},{'$set':{'status':'FAILED','verificationRejected':True,'verificationMessage':note,'verifiedAt':now(),'verifiedBy':current_user().get('email'),'updatedAt':now()}})
    db.members.update_one({'_id':m['_id']},{'$set':{'status':'PAYMENT_FAILED','adminMessage':note,'updatedAt':now()}})
    try: _send_member_email('membership_payment_failed',m,{'name':m.get('name',''),'message':note,'resumeUrl':f'{APP_URL}/member/dashboard','recreateUrl':f'{APP_URL}/membership/recreate/{m.get("recreateToken","")}'})
    except Exception: app.logger.exception('Payment rejection email failed')
    audit('MEMBERSHIP_PAYMENT_REJECTED','payment',payment_id,{'memberId':m['id']})
    return jsonify(ok=True,status='FAILED',message='Payment marked as unverified and the member has been notified.')

'''
assert marker in s
s=s.replace(marker,insert+marker,1)
# Ensure recreate token is created for new members
old="'resumeToken':secrets.token_urlsafe(32),'membershipNumber':'PENDING-'"
new="'resumeToken':secrets.token_urlsafe(32),'recreateToken':secrets.token_urlsafe(32),'membershipNumber':'PENDING-'"
assert old in s
s=s.replace(old,new,1)
# Existing EMAIL_PENDING duplicate ensure recreate token too
old="""if not resume_token:\n                    resume_token=secrets.token_urlsafe(32)\n                    db.members.update_one({'_id':existing['_id']},{'$set':{'resumeToken':resume_token,'updatedAt':now()}})\n                return jsonify"""
new="""if not resume_token:\n                    resume_token=secrets.token_urlsafe(32)\n                    db.members.update_one({'_id':existing['_id']},{'$set':{'resumeToken':resume_token,'updatedAt':now()}})\n                if not existing.get('recreateToken'):\n                    existing['recreateToken']=secrets.token_urlsafe(32); db.members.update_one({'_id':existing['_id']},{'$set':{'recreateToken':existing['recreateToken'],'updatedAt':now()}})\n                return jsonify"""
assert old in s
s=s.replace(old,new,1)
# DuplicateKey branch token
old="""if existing and not existing.get('resumeToken'):\n                existing['resumeToken']=secrets.token_urlsafe(32)\n                db.members.update_one({'_id':existing['_id']},{'$set':{'resumeToken':existing['resumeToken'],'updatedAt':now()}})\n            return jsonify"""
new="""if existing and not existing.get('resumeToken'):\n                existing['resumeToken']=secrets.token_urlsafe(32)\n                db.members.update_one({'_id':existing['_id']},{'$set':{'resumeToken':existing['resumeToken'],'updatedAt':now()}})\n            if existing and not existing.get('recreateToken'):\n                existing['recreateToken']=secrets.token_urlsafe(32); db.members.update_one({'_id':existing['_id']},{'$set':{'recreateToken':existing['recreateToken'],'updatedAt':now()}})\n            return jsonify"""
assert old in s
s=s.replace(old,new,1)
# Add manual payment info to portal
old="""renewals=[clean_doc(x) for x in db.membership_renewals.find({'memberId':member_id}).sort('createdAt',DESCENDING).limit(100)]\n    state=membership_state(m); window=renewal_window_days()"""
new="""renewals=[clean_doc(x) for x in db.membership_renewals.find({'memberId':member_id}).sort('createdAt',DESCENDING).limit(100)]\n    state=membership_state(m); window=renewal_window_days()\n    latest_payment=payments[0] if payments else None"""
assert old in s
s=s.replace(old,new,1)
old="""'certificateDriveId':m.get('certificateDriveId'),'renewalAvailable':state in"""
new="""'certificateDriveId':m.get('certificateDriveId'),'recreateUrl':f'{APP_URL}/membership/recreate/{m.get("recreateToken","")}' if m.get('recreateToken') else None,'latestPaymentStatus':latest_payment.get('status') if latest_payment else None,'latestPaymentId':latest_payment.get('id') if latest_payment else None,'latestMpesaReceiptNumber':latest_payment.get('mpesaReceiptNumber') if latest_payment else None,'renewalAvailable':state in"""
assert old in s
s=s.replace(old,new,1)
# Add payment failure email on subscribe catch
old="""db.members.update_one({'_id':m['_id']},{'$set':{'status':'PAYMENT_FAILED' if not is_renewal else state,'updatedAt':now()}})\n        return json_error('Unable to start membership payment.',502,'PAYMENT_FAILED')"""
new="""db.members.update_one({'_id':m['_id']},{'$set':{'status':'PAYMENT_FAILED' if not is_renewal else state,'updatedAt':now()}})\n        try:\n            _send_member_email('membership_payment_failed',m,{'name':m.get('name',''),'plan':plan.get('name',''),'message':'The M-Pesa payment prompt could not be completed. If money was deducted, do not pay again. Submit the M-Pesa transaction code from your message for GLDC verification.','resumeUrl':f'{APP_URL}/member/dashboard','recreateUrl':f'{APP_URL}/membership/recreate/{m.get("recreateToken","")}'})\n        except Exception: app.logger.exception('Payment failure email failed')\n        return json_error('We could not start the M-Pesa payment. If money was deducted, do not pay again—submit your M-Pesa transaction code for GLDC verification.',502,'PAYMENT_FAILED')"""
assert old in s
s=s.replace(old,new,1)
# Admin approve guard: require successful payment for initial/renewal
old="""if decision=='APPROVE':\n        plan=_membership_plan"""
new="""if decision=='APPROVE':\n        payment=db.payments.find_one({'id':m.get('paymentId')}) if m.get('paymentId') else None\n        if not payment or str(payment.get('status','')).upper()!='SUCCESSFUL':\n            return json_error('Payment must be successfully verified before membership approval.',409,'PAYMENT_NOT_VERIFIED')\n        plan=_membership_plan"""
assert old in s
s=s.replace(old,new,1)
# Add indexes for recreate and mpesa receipt
old="""db.membership_renewals.create_index([('paymentId',ASCENDING)], unique=True, sparse=True)\n    db.member_documents"""
new="""db.membership_renewals.create_index([('paymentId',ASCENDING)], unique=True, sparse=True)\n    db.members.create_index([('recreateToken',ASCENDING)], unique=True, sparse=True)\n    db.payments.create_index([('mpesaReceiptNumber',ASCENDING)], unique=True, sparse=True)\n    db.member_documents"""
assert old in s
s=s.replace(old,new,1)
# Email defaults add templates
old="""'membership_changes':{'subject':'GLDC Membership – action required','text':'Dear {{name}}, {{message}} Please use {{editUrl}} to update your details.','html':'<div style=\"font-family:Arial;max-width:640px;margin:auto\"><h2 style=\"color:#8B4A18\">Membership details need your attention</h2><p>Dear {{name}},</p><p>{{message}}</p><p><a href=\"{{editUrl}}\" style=\"background:#8B4A18;color:#fff;padding:12px 18px;text-decoration:none\">EDIT MY DETAILS</a></p></div>'},"""
new="""'membership_changes':{'subject':'GLDC Membership – action required','text':'Dear {{name}}, {{message}} Please use {{editUrl}} to update your details.','html':'<div style=\"font-family:Arial;max-width:640px;margin:auto\"><h2 style=\"color:#8B4A18\">Membership details need your attention</h2><p>Dear {{name}},</p><p>{{message}}</p><p><a href=\"{{editUrl}}\" style=\"background:#8B4A18;color:#fff;padding:12px 18px;text-decoration:none\">EDIT MY DETAILS</a></p></div>'},\n      'membership_payment_failed':{'subject':'GLDC Membership – payment needs attention','text':'Dear {{name}}, your membership payment could not be completed. {{message}} Continue: {{resumeUrl}}. If you already paid, submit the M-Pesa code for GLDC verification. If you need to start again, use the restart link provided by GLDC.','html':'<div style=\"font-family:Arial;max-width:640px;margin:auto\"><div style=\"padding:22px;background:#8B4A18;color:#fff\"><h2>GLDC Membership Payment</h2></div><div style=\"padding:24px\"><p>Dear <b>{{name}}</b>,</p><p>{{message}}</p><p><b>If you already paid:</b> do not pay again. Open your Member Portal and submit the M-Pesa transaction code shown in your M-Pesa message. GLDC will verify the transaction before approving your membership.</p><p><a href=\"{{resumeUrl}}\" style=\"background:#8B4A18;color:#fff;padding:12px 18px;text-decoration:none\">CONTINUE MY APPLICATION</a></p><p>If the application is incomplete and you need to create a new one, use the secure restart link supplied in your portal.</p></div></div>'},\n      'membership_payment_verified':{'subject':'GLDC Membership – payment verified','text':'Dear {{name}}, your M-Pesa payment {{mpesaCode}} has been verified by GLDC. Your application is now awaiting final approval.','html':'<div style=\"font-family:Arial;max-width:640px;margin:auto\"><h2 style=\"color:#8B4A18\">Payment verified ✓</h2><p>Dear <b>{{name}}</b>,</p><p>Your M-Pesa payment <b>{{mpesaCode}}</b> for <b>{{plan}}</b> has been verified by GLDC.</p><p>Your application is now awaiting final membership approval. Please do not make another payment.</p><p><a href=\"{{APP_URL}}/member/dashboard\">OPEN MEMBER PORTAL</a></p></div>'},"""
assert old in s
s=s.replace(old,new,1)
p.write_text(s)
