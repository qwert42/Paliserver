# encoding: utf-8

from datetime import date
import urllib2
from flask import session, request, redirect, url_for, flash, jsonify, send_from_directory, safe_join
from flask.ext.uploads import UploadNotAllowed, extension, os
from flask.templating import render_template
from sqlalchemy.exc import IntegrityError
from palis import app, db, paper_uploader, PATH_ENCODING, const
from palis.forms import LoginForm, ForwardForm, UploadForm, RegistrationForm, ResetPasswordForm
from palis.misc import gen_filename, sorted_paper_by_date, sorted_user_by_name, sorted_dispatch_by_date, requires_roles
from palis.models import User, PaperDispatchEntity, Paper

@app.before_first_request
def init_db(_=None):
    """initialize the users by querying the user table"""
    db.create_all()

    app.jinja_env.globals.update(user_list=User.query.all())


@app.before_request
def init_current_user(_=None):
    """get current user object by the username stored in session"""
    username = session.get('username', None)
    if username:
        user = User.query.filter_by(username=username).first()
        if user: # note that a user maybe deleted after he's logged in,
        # when this happens, username may still be in session,
        # but it's critical to test the user object's nullity
            app.jinja_env.globals.update(
                cur_uid=user._id,
                cur_username=username,
                unread_paper_count=len(filter(lambda pde: pde.status == 0x1 and not pde.forward_status,
                                              user.papers))
            )
            return

    app.jinja_env.globals.update(cur_uid=None, cur_username=None, unread_paper_count=None)


@app.route('/profile')
def profile():
    """render function for index page"""
    form = ResetPasswordForm(request.form)

    return render_template('profile.html', form=form)


@app.route('/admin', defaults={'active': 'users'})
@app.route('/admin/active/<active>', methods=['GET', 'POST'])
@requires_roles('admin')
def admin(active):
    """render function for administration page"""
    form = RegistrationForm(request.form)
    success, error = '', ''

    if request.method == 'POST' and form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if not user:
            user = User(form.username.data, form.password.data)
            success = 'User %s added successfully.' % user.username
        elif user.password == form.password.data:
            error = 'This ain\'t login form!'
        else:
            user.password = form.password.data
            success = 'Password modified successfully.'
        db.session.add(user)
        db.session.commit()

    users = filter(lambda user: user.username != 'admin', User.query.all())
    for user in users:
        user.force_statistics()

    return render_template('admin.html',
                           form=form,
                           success=success,
                           error=error,
                           users=sorted_user_by_name(users),
                           active=active)


@app.route('/change_pwd', methods=['POST'])
@requires_roles('user')
def change_pwd():
    """response function for password changing action"""
    form = ResetPasswordForm(request.form)
    success, error = '', ''

    if request.method == 'POST' and form.validate_on_submit():
        user = User.query.filter_by(_id=request.form['uid']).first()
        if user:
            if user.username == session['username']:
            # session must have `username', this is guaranteed by `need_login' decorator
                user.password = form.password.data
                db.session.commit()
                success = 'Password modified successfully'
            else:
                error = 'Mind your own business, will you?'
        else:
            error = 'You don\'t even exist :('

    return render_template('profile.html',
                           form=form,
                           success=success,
                           error=error)


@app.route('/login', methods=['GET', 'POST'])
def login():
    """render function for login page"""
    form = LoginForm(request.form)

    if 'username' in session and request.method != 'POST':
        return render_template('login.html',
                               banner_message='User %s has already logged in.' % session['username'],
                               form=form,
                               login_active=True)

    if request.method == 'POST' and form.validate_on_submit():
        logout()
        session['username'] = form.user.username
        session.permanent = False

        flash('you were successfully logged in')
        return redirect(url_for('show_list'))

    return render_template('login.html',
                           banner_message='Login for simple paper list service.',
                           form=form,
                           login_active=True)


@app.route('/', defaults={'active': 'from'})
@app.route('/user', defaults={'active': 'from'})
@app.route('/user/active/<active>')
@requires_roles('user')
def show_list(active):
    """render function for dispatch listing page"""
    user = User.query.filter_by(username=session['username']).first()
    for entity in user.papers + user.dispatches:
        entity.force_status_str()

    forward_form = ForwardForm()
    forward_form.users_selected.choices = [(u._id, u.username) for u in User.query.all()
                                           if u._id not in (user._id,
                                                            User.query.filter_by(username='admin').first()._id)]

    return render_template('papers.html',
                           pd_entities_to=sorted_dispatch_by_date(user.papers),
                           pd_entities_from=sorted_dispatch_by_date(user.dispatches),
                           forward_form=forward_form,
                           show_list_active=True,
                           active=active)


@app.route('/papers')
def view_papers():
    """render function for papers viewing page"""
    if 'username' in session and User.query.filter_by(username=session['username']).first():
        user = User.query.filter_by(username=session['username']).first()
        my_papers = user.uploaded_papers

        forward_form = ForwardForm()
        forward_form.users_selected.choices = [(u._id, u.username) for u in User.query.all()
                                               if u._id not in (user._id,
                                                                User.query.filter_by(username='admin').first()._id)]
    else:
        my_papers = None
        forward_form = None

    all_papers = Paper.query.all()
    for paper in all_papers:
        paper.force_statistics_data()

    return render_template('view_papers.html',
                           paper_all=sorted_paper_by_date(all_papers),
                           paper_my=sorted_paper_by_date(my_papers),
                           forward_form=forward_form,
                           view_papers_active=True,
                           logged_in=forward_form) # if logged in, form is always True



@app.route('/read', methods=['POST'])
def read_paper():
    """response function for paper status marking action"""
    pde_id = request.form['pde_id']
    pde = PaperDispatchEntity.query.filter_by(_id=pde_id).first()

    if pde.status == 0x1:
        pde.status = 0x2
    elif pde.status == 0x2:
        pde.status = 0x3

    db.session.commit()

    return redirect(url_for('show_list'))


@app.route('/forward', methods=['POST'])
@requires_roles('user')
def forward_paper():
    """response function for paper forwarding action"""
    for user_id in request.form['selected_uid'].split(','):
        new_pde = PaperDispatchEntity(request.form['from_uid'],
                                      user_id,
                                      request.form['paper_id'],
                                      0x1,
                                      date.today())
        app.logger.info(new_pde)
        db.session.add(new_pde)
        try:
            db.session.commit()
            app.logger.info('db committed')
        except IntegrityError:
            db.session.rollback()
            app.logger.error('%s already in db' % new_pde)

    return redirect(url_for('show_list'))


@app.route('/validate_paper', methods=['GET'])
def validate_paper():
    """response function for paper validation requesting action"""
    title = request.args.get('title', '', type=unicode)
    author = request.args.get('author', '', type=unicode)

    if not title or not author:
        return jsonify(result='incomplete')
    elif Paper.query.filter_by(title=title, author=author).first():
        return jsonify(result='exist')
    return jsonify(result='proceed')


@app.route('/upload', methods=['GET', 'POST'])
@requires_roles('user')
def upload_paper():
    """response function for paper uploading action"""
    form = UploadForm(form=request.form)

    if request.method == 'POST' and form.validate_on_submit():
        filename, url = '', ''

        if 'paper' in request.files and request.files['paper'].filename:
            try:
                filename = gen_filename(form.title.data, form.author.data) +\
                                        '.' +\
                                        extension(request.files['paper'].filename)
                paper_uploader.save(request.files['paper'], name=filename)
            except UploadNotAllowed:
                return render_template('upload.html',
                                       error='Please select file to upload.',
                                       form=form, upload_paper_active=True)
        else:
            url = form.url.data

        paper = Paper(form.author.data, form.title.data, filename, url, date.today(),
                      User.query.filter_by(username=session['username']).first()._id)
        db.session.add(paper)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # db.session.delete(paper)
            return render_template('upload.html', error='Filename already exists.', form=form,
                                   upload_paper_active=True)

        flash('Paper uploaded successfully.')
        return render_template('upload.html', success='Paper uploaded successfully.', form=UploadForm(),
                               upload_paper_active=True)

    return render_template('upload.html', form=form, upload_paper_active=True)


@app.route('/download', methods=['GET'])
def download_paper():
    """response function for paper downloading action"""
    paper_id = request.args['paper_id']
    paper = Paper.query.filter_by(_id=paper_id).first()

    if not paper.filename:
        return redirect(urllib2.unquote(paper.url))
    else:
        # note that the encoding of the paths differs in windows and linux,
        # and everything retrieved from database are unicode,
        # and http doesn't care for unicode
        return send_from_directory(const.paper_dir_path,
                                   Paper.query.filter_by(_id=paper_id).first().filename.encode(PATH_ENCODING),
                                   as_attachment=True)


@app.route('/withdraw', methods=['POST'])
def withdraw_dispatch():
    """response function for dispatch withdraw action"""
    pde_id = request.form['pde_id']
    pde = PaperDispatchEntity.query.filter_by(_id=pde_id).first()

    status = request.form['status']
    if status == 'refuse':
        pde.forward_status = 0x1
        pde.status = 0x1 # revert status to unread
    else:
        db.session.delete(PaperDispatchEntity.query.filter_by(_id=pde_id).first())

    db.session.commit()

    return redirect(url_for('show_list', active='to' if status != 'refuse' else 'from'))


@app.route('/delete', methods=['POST'])
@requires_roles('user')
def delete_paper():
    """response function for paper deleting action"""
    paper = Paper.query.filter_by(_id=request.form['paper_id']).first()
    for pde in paper.dispatched_entities:
        db.session.delete(pde)
    db.session.delete(Paper.query.filter_by(_id=request.form['paper_id']).first())

    try:
        os.remove(safe_join(const.paper_dir_path, paper.filename))
    except OSError:
        pass

    db.session.commit()

    return redirect(url_for('view_papers'))


@app.route('/delete_user', methods=['POST'])
@requires_roles('admin')
def delete_user():
    """response function for user deleting action"""
    user = User.query.filter_by(_id=request.form['user_id']).first()
    for pde in user.papers:
        db.session.delete(pde)
    db.session.delete(user)
    db.session.commit()

    return redirect(url_for('admin', active='users'))


@app.route('/redispatch', methods=['POST'])
def redispatch():
    """response function for redispatching action"""
    pde = PaperDispatchEntity.query.filter_by(_id=request.form['pde_id']).first()
    pde.status = 0x1
    pde.forward_status = 0x0
    pde.dispatch_date = date.today()

    db.session.commit()

    return redirect(url_for('show_list', active='to'))


@app.route('/logout')
def logout():
    """response function for logging out action"""
    if not session.get('username', None):
        return redirect(url_for('profile'))

    app.logger.info('user %s logged out' % session['username'])

    session.pop('username', None)

    return redirect(url_for('profile'))



if __name__ == '__main__':
    app.debug = True
    app.run(host='0.0.0.0')

